from __future__ import print_function

import sys
import logging
import argparse
import torchvision.datasets as dset
from torch.utils.data import Dataset, TensorDataset, ConcatDataset
from sklearn import metrics
from scipy import stats

from collections import OrderedDict

import random
import os
import os.path
import gzip
import pickle
from functools import partial
from pathlib import Path

import numpy as np
import torch
import torchvision.transforms as transforms

from fvcore.common.checkpoint import Checkpointer as fvCheckpointer
from fvcore.common.config import CfgNode

from .taskonomy_dataset import get_datasets
from . import load_ops

from pytorch_msssim import ssim, ms_ssim, SSIM, MS_SSIM


cat_channels = partial(torch.cat, dim=1)

logger = logging.getLogger(__name__)


def get_project_root() -> Path:
    """
    Returns the root path of the project.
    """
    return Path(__file__).parent.parent


def iter_flatten(iterable):
    """
    Flatten a potentially deeply nested python list
    """
    # taken from https://rightfootin.blogspot.com/2006/09/more-on-python-flatten.html
    it = iter(iterable)
    for e in it:
        if isinstance(e, (list, tuple)):
            for f in iter_flatten(e):
                yield f
        else:
            yield e


def default_argument_parser():
    """
    Returns the argument parser with the default options.

    Inspired by the implementation of FAIR's detectron2
    """

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config-file", default=None, metavar="FILE", help="Path to config file")
    parser.add_argument(
        "opts",
        help="Modify config options using the command-line",
        default=None,
        nargs=argparse.REMAINDER,
    )
    parser.add_argument("--datapath", default=None, metavar="FILE", help="Path to the folder with train/test data folders")
    return parser


def parse_args(parser=default_argument_parser(), args=sys.argv[1:]):
    if "-f" in args:
        args = args[2:]
    return parser.parse_args(args)


def pairwise(iterable):
    """
    Iterate pairwise over list.

    from https://stackoverflow.com/questions/5389507/iterating-over-every-two-elements-in-a-list
    """
    "s -> (s0, s1), (s2, s3), (s4, s5), ..."
    a = iter(iterable)
    return zip(a, a)

def load_config(path):
    with open(path) as f:
        config = CfgNode.load_cfg(f)

    return config

def load_default_config():
    config_paths = "configs/predictor_config.yaml"

    config_path_full = os.path.join(
        *(
            [get_project_root()] + config_paths.split('/')
        )
    )

    return load_config(config_path_full)

def get_config_from_args(args=None):
    """
    Parses command line arguments and merges them with the defaults
    from the config file.

    Prepares experiment directories.

    Args:
        args: args from a different argument parser than the default one.
    """

    if args is None:
        args = parse_args()
    logger.info("Command line args: {}".format(args))

    if args.config_file is None:
        config = load_default_config()
    else:
        config = load_config(path=args.config_file)

    # Override file args with ones from command line
    try:
        for arg, value in pairwise(args.opts):
            if "." in arg:
                arg1, arg2 = arg.split(".")
                config[arg1][arg2] = type(config[arg1][arg2])(value)
            else:
                if arg in config:
                    t = type(config[arg])
                elif value.isnumeric():
                    t = int
                else:
                    t = str
                config[arg] = t(value)

        # load config file
        config.set_new_allowed(True)
        config.merge_from_list(args.opts)

    except AttributeError:
        for arg, value in pairwise(args):
            config[arg] = value

    if args.datapath is not None:
        config.train_data_file = os.path.join(args.datapath, 'train.json')
        config.test_data_file = os.path.join(args.datapath, 'test.json')
    else:
        config.train_data_file = None
        config.test_data_file = None

    # prepare the output directories
    config.save = "{}/{}/{}/{}/{}/{}".format(
        config.out_dir,
        config.config_type,
        config.search_space,
        config.dataset,
        config.predictor,
        config.seed,
    )
    config.data = "{}/data".format(get_project_root())

    create_exp_dir(config.save)
    create_exp_dir(config.save + "/search")  # required for the checkpoints
    create_exp_dir(config.save + "/eval")

    return config

def load_ninapro(path, whichset):
    data_str = 'ninapro_' + whichset + '.npy'
    label_str = 'label_' + whichset + '.npy'

    data = np.load(os.path.join(path, data_str),
                             encoding="bytes", allow_pickle=True)
    labels = np.load(os.path.join(path, label_str), encoding="bytes", allow_pickle=True)

    data = np.transpose(data, (0, 2, 1))
    data = data[:, None, :, :]
    data = torch.from_numpy(data.astype(np.float32))
    labels = torch.from_numpy(labels.astype(np.int64))

    all_data = TensorDataset(data, labels)
    return all_data

def get_train_val_loaders(config):
    """
    Constructs the dataloaders and transforms for training, validation and test data.
    """
    data = config.data
    dataset = config.dataset
    seed = config.seed
    if dataset == "cifar10":
        train_transform, valid_transform = _data_transforms_cifar10(config)
        train_data = dset.CIFAR10(
            root=data, train=True, download=True, transform=train_transform
        )
        test_data = dset.CIFAR10(
            root=data, train=False, download=True, transform=valid_transform
        )
    elif dataset == "cifar100":
        train_transform, valid_transform = _data_transforms_cifar100(config)
        train_data = dset.CIFAR100(
            root=data, train=True, download=True, transform=train_transform
        )
        test_data = dset.CIFAR100(
            root=data, train=False, download=True, transform=valid_transform
        )
    elif dataset == "svhn":
        train_transform, valid_transform = _data_transforms_svhn(config)
        train_data = dset.SVHN(
            root=data, split="train", download=True, transform=train_transform
        )
        test_data = dset.SVHN(
            root=data, split="test", download=True, transform=valid_transform
        )
    elif dataset == "ImageNet16-120":
        from naslib.utils.DownsampledImageNet import ImageNet16

        train_transform, valid_transform = _data_transforms_ImageNet_16_120(config)
        data_folder = f"{data}/{dataset}"
        train_data = ImageNet16(
            root=data_folder,
            train=True,
            transform=train_transform,
            use_num_of_class_only=120,
        )
        test_data = ImageNet16(
            root=data_folder,
            train=False,
            transform=valid_transform,
            use_num_of_class_only=120,
        )
    elif dataset == "scifar100":
        data_file = os.path.join(data, 's2_cifar100.gz')
        with gzip.open(data_file, 'rb') as f:
            dataset = pickle.load(f)

        train_img = torch.from_numpy(
            dataset["train"]["images"][:, :, :, :].astype(np.float32))
        train_labels = torch.from_numpy(
            dataset["train"]["labels"].astype(np.int64))
        train_data = TensorDataset(train_img, train_labels)
        test_img = torch.from_numpy(
            dataset["test"]["images"][:, :, :, :].astype(np.float32))
        test_labels = torch.from_numpy(
            dataset["test"]["labels"].astype(np.int64))
        test_data = TensorDataset(test_img, test_labels)

        train_transform, valid_transform = None, None
    elif dataset == "ninapro":
        path = os.path.join(data, 'ninapro')

        trainset = load_ninapro(path, 'train')
        valset = load_ninapro(path, 'val')
        train_data = ConcatDataset([trainset, valset])
        test_data = load_ninapro(path, 'test')

        train_transform, valid_transform = None, None
    elif dataset == 'jigsaw':
        cfg = get_jigsaw_configs()

        train_data, val_data, test_data = get_datasets(cfg)

        train_transform = cfg['train_transform_fn']
        valid_transform = cfg['val_transform_fn']

    elif dataset == 'class_object':
        cfg = get_class_object_configs()

        train_data, val_data, test_data = get_datasets(cfg)

        train_transform = cfg['train_transform_fn']
        valid_transform = cfg['val_transform_fn']

    elif dataset == 'class_scene':
        cfg = get_class_scene_configs()

        train_data, val_data, test_data = get_datasets(cfg)

        train_transform = cfg['train_transform_fn']
        valid_transform = cfg['val_transform_fn']

    elif dataset == 'autoencoder':
        cfg = get_autoencoder_configs()

        train_data, val_data, test_data = get_datasets(cfg)

        train_transform = cfg['train_transform_fn']
        valid_transform = cfg['val_transform_fn']
    
    elif dataset == 'segmentsemantic':
        cfg = get_segmentsemantic_configs()

        train_data, val_data, test_data = get_datasets(cfg)

        train_transform = cfg['train_transform_fn']
        valid_transform = cfg['val_transform_fn']

    elif dataset == 'normal':
        cfg = get_normal_configs()

        train_data, val_data, test_data = get_datasets(cfg)

        train_transform = cfg['train_transform_fn']
        valid_transform = cfg['val_transform_fn']
    
    elif dataset == 'room_layout':
        cfg = get_room_layout_configs()

        train_data, val_data, test_data = get_datasets(cfg)

        train_transform = cfg['train_transform_fn']
        valid_transform = cfg['val_transform_fn']

    else:
        raise ValueError("Unknown dataset: {}".format(dataset))

    num_train = len(train_data)
    indices = list(range(num_train))
    split = int(np.floor(config.train_portion * num_train))

    train_queue = torch.utils.data.DataLoader(
        train_data,
        batch_size=config.batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices[:split]),
        pin_memory=True,
        num_workers=0,
        worker_init_fn=np.random.seed(seed+1),
    )

    valid_queue = torch.utils.data.DataLoader(
        train_data,
        batch_size=config.batch_size,
        sampler=torch.utils.data.sampler.SubsetRandomSampler(indices[split:num_train]),
        pin_memory=True,
        num_workers=0,
        worker_init_fn=np.random.seed(seed),
    )

    test_queue = torch.utils.data.DataLoader(
        test_data,
        batch_size=config.batch_size,
        shuffle=False,
        pin_memory=True,
        num_workers=0,
        worker_init_fn=np.random.seed(seed),
    )

    return train_queue, valid_queue, test_queue, train_transform, valid_transform


def _data_transforms_cifar10(args):
    CIFAR_MEAN = [0.49139968, 0.48215827, 0.44653124]
    CIFAR_STD = [0.24703233, 0.24348505, 0.26158768]

    train_transform = transforms.Compose(
        [
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(CIFAR_MEAN, CIFAR_STD),
        ]
    )
    if hasattr(args, 'cutout') and args.cutout == True:
        train_transform.transforms.append(Cutout(args.cutout_length, args.cutout_prob))

    valid_transform = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(CIFAR_MEAN, CIFAR_STD),
        ]
    )
    return train_transform, valid_transform


def _data_transforms_svhn(args):
    SVHN_MEAN = [0.4377, 0.4438, 0.4728]
    SVHN_STD = [0.1980, 0.2010, 0.1970]

    train_transform = transforms.Compose(
        [
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(SVHN_MEAN, SVHN_STD),
        ]
    )
    if hasattr(args, 'cutout') and args.cutout == True:
        train_transform.transforms.append(Cutout(args.cutout_length, args.cutout_prob))

    valid_transform = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(SVHN_MEAN, SVHN_STD),
        ]
    )
    return train_transform, valid_transform


def _data_transforms_cifar100(args):
    CIFAR_MEAN = [0.5071, 0.4865, 0.4409]
    CIFAR_STD = [0.2673, 0.2564, 0.2762]

    train_transform = transforms.Compose(
        [
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(CIFAR_MEAN, CIFAR_STD),
        ]
    )
    if hasattr(args, 'cutout') and args.cutout == True:
        train_transform.transforms.append(Cutout(args.cutout_length, args.cutout_prob))

    valid_transform = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(CIFAR_MEAN, CIFAR_STD),
        ]
    )
    return train_transform, valid_transform


def _data_transforms_ImageNet_16_120(args):
    IMAGENET16_MEAN = [x / 255 for x in [122.68, 116.66, 104.01]]
    IMAGENET16_STD = [x / 255 for x in [63.22, 61.26, 65.09]]

    train_transform = transforms.Compose(
        [
            transforms.RandomCrop(16, padding=2),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(IMAGENET16_MEAN, IMAGENET16_STD),
        ]
    )
    if hasattr(args, 'cutout') and args.cutout == True:
        train_transform.transforms.append(Cutout(args.cutout_length, args.cutout_prob))

    valid_transform = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(IMAGENET16_MEAN, IMAGENET16_STD),
        ]
    )
    return train_transform, valid_transform


def get_jigsaw_configs():
    cfg = {}
    
    cfg['task_name'] = 'jigsaw'

    cfg['input_dim'] = (255, 255)
    cfg['target_num_channels'] = 9
    
    cfg['dataset_dir'] = os.path.join(get_project_root(), "data", "taskonomydata_mini")
    cfg['data_split_dir'] = os.path.join(get_project_root(), "data", "final5K_splits")
    
    cfg['train_filenames'] = 'train_filenames_final5k.json'
    cfg['val_filenames'] = 'val_filenames_final5k.json'
    cfg['test_filenames'] = 'test_filenames_final5k.json'
    
    cfg['target_dim'] = 1000
    cfg['target_load_fn'] = load_ops.random_jigsaw_permutation
    cfg['target_load_kwargs'] = {'classes': cfg['target_dim']}
    
    cfg['train_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.RandomHorizontalFlip(0.5),
        load_ops.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        load_ops.RandomGrayscale(0.3),
        load_ops.MakeJigsawPuzzle(classes=cfg['target_dim'], mode='max', tile_dim=(64, 64), centercrop=0.9, norm=False, totensor=True),
    ])
    
    cfg['val_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.RandomHorizontalFlip(0.5),
        load_ops.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        load_ops.RandomGrayscale(0.3),
        load_ops.MakeJigsawPuzzle(classes=cfg['target_dim'], mode='max', tile_dim=(64, 64), centercrop=0.9, norm=False, totensor=True),
    ])
    
    cfg['test_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.RandomHorizontalFlip(0.5),
        load_ops.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        load_ops.RandomGrayscale(0.3),
        load_ops.MakeJigsawPuzzle(classes=cfg['target_dim'], mode='max', tile_dim=(64, 64), centercrop=0.9, norm=False, totensor=True),
    ])
    return cfg


def get_class_object_configs():
    cfg = {}
    
    cfg['task_name'] = 'class_object'

    cfg['input_dim'] = (256, 256)
    cfg['input_num_channels'] = 3
    
    cfg['dataset_dir'] = os.path.join(get_project_root(), "data", "taskonomydata_mini")
    cfg['data_split_dir'] = os.path.join(get_project_root(), "data", "final5K_splits")
    
    cfg['train_filenames'] = 'train_filenames_final5k.json'
    cfg['val_filenames'] = 'val_filenames_final5k.json'
    cfg['test_filenames'] = 'test_filenames_final5k.json'
    
    cfg['target_dim'] = 75
    
    cfg['target_load_fn'] = load_ops.load_class_object_logits
    
    cfg['target_load_kwargs'] = {'selected': True if cfg['target_dim'] < 1000 else False, 'final5k': True if cfg['data_split_dir'].split('/')[-1] == 'final5k' else False}
    
    cfg['demo_kwargs'] = {'selected': True if cfg['target_dim'] < 1000 else False, 'final5k': True if cfg['data_split_dir'].split('/')[-1] == 'final5k' else False}
    
    cfg['normal_params'] = {'mean': [0.5224, 0.5222, 0.5221], 'std': [0.2234, 0.2235, 0.2236]}
    
    cfg['train_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.RandomHorizontalFlip(0.5),
        load_ops.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['val_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['test_transform_fn'] = load_ops.Compose(cfg['task_name'], [
       load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    return cfg


def get_class_scene_configs():
    cfg = {}
    
    cfg['task_name'] = 'class_scene'

    cfg['input_dim'] = (256, 256)
    cfg['input_num_channels'] = 3
    
    cfg['dataset_dir'] = os.path.join(get_project_root(), "data", "taskonomydata_mini")
    cfg['data_split_dir'] = os.path.join(get_project_root(), "data", "final5K_splits")
    
    cfg['train_filenames'] = 'train_filenames_final5k.json'
    cfg['val_filenames'] = 'val_filenames_final5k.json'
    cfg['test_filenames'] = 'test_filenames_final5k.json'
    
    cfg['target_dim'] = 47
    
    cfg['target_load_fn'] = load_ops.load_class_scene_logits
    
    cfg['target_load_kwargs'] = {'selected': True if cfg['target_dim'] < 365 else False, 'final5k': True if cfg['data_split_dir'].split('/')[-1] == 'final5k' else False}
    
    cfg['demo_kwargs'] = {'selected': True if cfg['target_dim'] < 365 else False, 'final5k': True if cfg['data_split_dir'].split('/')[-1] == 'final5k' else False}
    
    cfg['normal_params'] = {'mean': [0.5224, 0.5222, 0.5221], 'std': [0.2234, 0.2235, 0.2236]}
    
    cfg['train_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.RandomHorizontalFlip(0.5),
        load_ops.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['val_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['test_transform_fn'] = load_ops.Compose(cfg['task_name'], [
       load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    return cfg


def get_autoencoder_configs():
    
    cfg = {}
    
    cfg['task_name'] = 'autoencoder'
    
    cfg['input_dim'] = (256, 256)
    cfg['input_num_channels'] = 3
    
    cfg['target_dim'] = (256, 256)
    cfg['target_channel'] = 3

    cfg['dataset_dir'] = os.path.join(get_project_root(), "data", "taskonomydata_mini")
    cfg['data_split_dir'] = os.path.join(get_project_root(), "data", "final5K_splits")
   
    cfg['train_filenames'] = 'train_filenames_final5k.json'
    cfg['val_filenames'] = 'val_filenames_final5k.json'
    cfg['test_filenames'] = 'test_filenames_final5k.json'
    
    cfg['target_load_fn'] = load_ops.load_raw_img_label
    cfg['target_load_kwargs'] = {}
    
    cfg['normal_params'] = {'mean': [0.5, 0.5, 0.5], 'std': [0.5, 0.5, 0.5]}
    
    cfg['train_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.RandomHorizontalFlip(0.5),
        load_ops.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['val_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['test_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    return cfg

def get_segmentsemantic_configs():
    
    cfg = {}
    
    cfg['task_name'] = 'segmentsemantic'
    
    cfg['input_dim'] = (256, 256)
    cfg['input_num_channels'] = 3 
    
    cfg['target_dim'] = (256, 256)
    cfg['target_num_channel'] = 17

    cfg['dataset_dir'] = os.path.join(get_project_root(), "data", "taskonomydata_mini")
    cfg['data_split_dir'] = os.path.join(get_project_root(), "data", "final5K_splits")
   
    cfg['train_filenames'] = 'train_filenames_final5k.json'
    cfg['val_filenames'] = 'val_filenames_final5k.json'
    cfg['test_filenames'] = 'test_filenames_final5k.json'
    
    cfg['target_load_fn'] = load_ops.semantic_segment_label
    cfg['target_load_kwargs'] = {}
    
    cfg['normal_params'] = {'mean': [0.5, 0.5, 0.5], 'std': [0.5, 0.5, 0.5]}
    
    cfg['train_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.RandomHorizontalFlip(0.5),
        load_ops.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['val_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['test_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    return cfg

def get_normal_configs():
    
    cfg = {}
    
    cfg['task_name'] = 'normal'
    
    cfg['input_dim'] = (256, 256)
    cfg['input_num_channels'] = 3 
    
    cfg['target_dim'] = (256, 256)
    cfg['target_channel'] = 3

    cfg['dataset_dir'] = os.path.join(get_project_root(), "data", "taskonomydata_mini")
    cfg['data_split_dir'] = os.path.join(get_project_root(), "data", "final5K_splits")
   
    cfg['train_filenames'] = 'train_filenames_final5k.json'
    cfg['val_filenames'] = 'val_filenames_final5k.json'
    cfg['test_filenames'] = 'test_filenames_final5k.json'
    
    cfg['target_load_fn'] = load_ops.load_raw_img_label
    cfg['target_load_kwargs'] = {}
    
    cfg['normal_params'] = {'mean': [0.5, 0.5, 0.5], 'std': [0.5, 0.5, 0.5]}
    
    cfg['train_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        # load_ops.RandomHorizontalFlip(0.5),
        # load_ops.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['val_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['test_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    return cfg

def get_room_layout_configs():
    
    cfg = {}
    
    cfg['task_name'] = 'room_layout'
    
    cfg['input_dim'] = (256, 256)
    cfg['input_num_channels'] = 3 
    
    cfg['target_dim'] = 9

    cfg['dataset_dir'] = os.path.join(get_project_root(), "data", "taskonomydata_mini")
    cfg['data_split_dir'] = os.path.join(get_project_root(), "data", "final5K_splits")
   
    cfg['train_filenames'] = 'train_filenames_final5k.json'
    cfg['val_filenames'] = 'val_filenames_final5k.json'
    cfg['test_filenames'] = 'test_filenames_final5k.json'
    
    cfg['target_load_fn'] = load_ops.point_info2room_layout
    # cfg['target_load_fn'] = load_ops.room_layout
    cfg['target_load_kwargs'] = {}
    
    cfg['normal_params'] = {'mean': [0.5224, 0.5222, 0.5221], 'std': [0.2234, 0.2235, 0.2236]}
    
    cfg['train_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        # load_ops.RandomHorizontalFlip(0.5),
        load_ops.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['val_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    
    cfg['test_transform_fn'] = load_ops.Compose(cfg['task_name'], [
        load_ops.ToPILImage(),
        load_ops.Resize(list(cfg['input_dim'])),
        load_ops.ToTensor(),
        load_ops.Normalize(**cfg['normal_params']),
    ])
    return cfg



class TensorDatasetWithTrans(Dataset):
    """
    TensorDataset with support of transforms.
    """

    def __init__(self, tensors, transform=None):
        assert all(tensors[0].size(0) == tensor.size(0) for tensor in tensors)
        self.tensors = tensors
        self.transform = transform

    def __getitem__(self, index):
        x = self.tensors[0][index]

        if self.transform:
            x = self.transform(x)

        y = self.tensors[1][index]

        return x, y

    def __len__(self):
        return self.tensors[0].size(0)


def set_seed(seed):
    """
    Set the seeds for all used libraries.
    """
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.deterministic = True
        torch.cuda.manual_seed_all(seed)


def get_last_checkpoint(config, search=True):
    """
    Finds the latest checkpoint in the experiment directory.

    Args:
        config (AttrDict): The config from config file.
        search (bool): Search or evaluation checkpoint

    Returns:
        (str): The path to the latest checkpoint file.
    """
    try:
        path = os.path.join(
            config.save, "search" if search else "eval", "last_checkpoint"
        )
        with open(path, "r") as f:
            checkpoint_name = f.readline()
        return os.path.join(
            config.save, "search" if search else "eval", checkpoint_name
        )
    except:
        return ""
    

def accuracy(output, target, topk=(1,)):
    """
    Calculate the accuracy given the softmax output and the target.
    """
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].reshape(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


def accuracy_class_object(output, target, topk=(1,)):
    """
    Calculate the accuracy given the softmax output and the target.
    """
    target = target.argmax(dim=1)
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].reshape(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

def accuracy_class_scene(output, target, topk=(1,)):
    """
    Calculate the accuracy given the softmax output and the target.
    """
    target = target.argmax(dim=1)
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].reshape(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


def accuracy_autoencoder(output, target, topk=(1,)):
    ssim_loss= SSIM(data_range=1, size_average=True, channel=3)
    res = ssim_loss(output, target)
    """
    Calculate the accuracy given the softmax output and the target.
    """
    return res, res


def count_parameters_in_MB(model):
    """
    Returns the model parameters in mega byte.
    """
    return (
        np.sum(
            np.prod(v.size())
            for name, v in model.named_parameters()
            if "auxiliary" not in name
        )
        / 1e6
    )


def log_args(args):
    """
    Log the args in a nice way.
    """
    for arg, val in args.items():
        logger.info(arg + "." * (50 - len(arg) - len(str(val))) + str(val))


def create_exp_dir(path):
    """
    Create the experiment directories.
    """
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
    logger.info("Experiment dir : {}".format(path))


def cross_validation(
    xtrain, ytrain, predictor, split_indices, score_metric="kendalltau"
):
    validation_score = []

    for train_indices, validation_indices in split_indices:
        xtrain_i = [xtrain[j] for j in train_indices]
        ytrain_i = [ytrain[j] for j in train_indices]
        xval_i = [xtrain[j] for j in train_indices]
        yval_i = [ytrain[j] for j in train_indices]

        predictor.fit(xtrain_i, ytrain_i)
        ypred_i = predictor.query(xval_i)

        # If the predictor is an ensemble, take the mean
        if len(ypred_i.shape) > 1:
            ypred_i = np.mean(ypred_i, axis=0)

        # use Pearson correlation to be the metric -> maximise Pearson correlation
        if score_metric == "pearson":
            score_i = np.abs(np.corrcoef(yval_i, ypred_i)[1, 0])
        elif score_metric == "mae":
            score_i = np.mean(abs(ypred_i - yval_i))
        elif score_metric == "rmse":
            score_i = metrics.mean_squared_error(yval_i, ypred_i, squared=False)
        elif score_metric == "spearman":
            score_i = stats.spearmanr(yval_i, ypred_i)[0]
        elif score_metric == "kendalltau":
            score_i = stats.kendalltau(yval_i, ypred_i)[0]
        elif score_metric == "kt_2dec":
            score_i = stats.kendalltau(yval_i, np.round(ypred_i, decimals=2))[0]
        elif score_metric == "kt_1dec":
            score_i = stats.kendalltau(yval_i, np.round(ypred_i, decimals=1))[0]

        validation_score.append(score_i)

    return np.mean(validation_score)


def generate_kfold(n, k):
    """
    Input:
        n: number of training examples
        k: number of folds
    Returns:
        kfold_indices: a list of len k. Each entry takes the form
        (training indices, validation indices)
    """
    assert k >= 2
    kfold_indices = []

    indices = np.array(range(n))
    fold_size = n // k

    fold_indices = [indices[i * fold_size : (i + 1) * fold_size] for i in range(k - 1)]
    fold_indices.append(indices[(k - 1) * fold_size :])

    for i in range(k):
        training_indices = [fold_indices[j] for j in range(k) if j != i]
        validation_indices = fold_indices[i]
        kfold_indices.append((np.concatenate(training_indices), validation_indices))

    return kfold_indices


def compute_scores(ytest, test_pred):
    ytest = np.array(ytest)
    test_pred = np.array(test_pred)
    METRICS = [
        "mae",
        "rmse",
        "pearson",
        "spearman",
        "kendalltau",
        "kt_2dec",
        "kt_1dec",
        "full_ytest",
        "full_testpred",
    ]
    metrics_dict = {}

    try:
        metrics_dict["mae"] = np.mean(abs(test_pred - ytest))
        metrics_dict["rmse"] = metrics.mean_squared_error(
            ytest, test_pred, squared=False
        )
        metrics_dict["pearson"] = np.abs(np.corrcoef(ytest, test_pred)[1, 0])
        metrics_dict["spearman"] = stats.spearmanr(ytest, test_pred)[0]
        metrics_dict["kendalltau"] = stats.kendalltau(ytest, test_pred)[0]
        metrics_dict["kt_2dec"] = stats.kendalltau(
            ytest, np.round(test_pred, decimals=2)
        )[0]
        metrics_dict["kt_1dec"] = stats.kendalltau(
            ytest, np.round(test_pred, decimals=1)
        )[0]
        for k in [10, 20]:
            top_ytest = np.array(
                [y > sorted(ytest)[max(-len(ytest), -k - 1)] for y in ytest]
            )
            top_test_pred = np.array(
                [
                    y > sorted(test_pred)[max(-len(test_pred), -k - 1)]
                    for y in test_pred
                ]
            )
            metrics_dict["precision_{}".format(k)] = (
                sum(top_ytest & top_test_pred) / k
            )
        metrics_dict["full_ytest"] = ytest.tolist()
        metrics_dict["full_testpred"] = test_pred.tolist()

    except:
        for metric in METRICS:
            metrics_dict[metric] = float("nan")
    if np.isnan(metrics_dict["pearson"]) or not np.isfinite(
        metrics_dict["pearson"]
    ):
        logger.info("Error when computing metrics. ytest and test_pred are:")
        logger.info(ytest)
        logger.info(test_pred)

    return metrics_dict

class AttrDict(dict):
    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self


class AverageMeterGroup:
    """Average meter group for multiple average meters, ported from Naszilla repo."""

    def __init__(self):
        self.meters = OrderedDict()

    def update(self, data, n=1):
        for k, v in data.items():
            if k not in self.meters:
                self.meters[k] = NamedAverageMeter(k, ":4f")
            self.meters[k].update(v, n=n)

    def __getattr__(self, item):
        return self.meters[item]

    def __getitem__(self, item):
        return self.meters[item]

    def __str__(self):
        return "  ".join(str(v) for v in self.meters.values())

    def summary(self):
        return "  ".join(v.summary() for v in self.meters.values())


class NamedAverageMeter:
    """Computes and stores the average and current value, ported from naszilla repo"""

    def __init__(self, name, fmt=":f"):
        """
        Initialization of AverageMeter
        Parameters
        ----------
        name : str
            Name to display.
        fmt : str
            Format string to print the values.
        """
        self.name = name
        self.fmt = fmt
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = "{name} {val" + self.fmt + "} ({avg" + self.fmt + "})"
        return fmtstr.format(**self.__dict__)

    def summary(self):
        fmtstr = "{name}: {avg" + self.fmt + "}"
        return fmtstr.format(**self.__dict__)


class AverageMeter(object):
    def __init__(self):
        self.reset()

    def reset(self):
        self.avg = 0
        self.sum = 0
        self.cnt = 0

    def update(self, val, n=1):
        self.sum += val * n
        self.cnt += n
        self.avg = self.sum / self.cnt


class Cutout(object):
    def __init__(self, length, prob=1.0):
        self.length = length
        self.prob = prob

    def __call__(self, img):
        if np.random.binomial(1, self.prob):
            h, w = img.size(1), img.size(2)
            mask = np.ones((h, w), np.float32)
            y = np.random.randint(h)
            x = np.random.randint(w)

            y1 = np.clip(y - self.length // 2, 0, h)
            y2 = np.clip(y + self.length // 2, 0, h)
            x1 = np.clip(x - self.length // 2, 0, w)
            x2 = np.clip(x + self.length // 2, 0, w)

            mask[y1:y2, x1:x2] = 0.0
            mask = torch.from_numpy(mask)
            mask = mask.expand_as(img)
            img *= mask
        return img


from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple
from fvcore.common.file_io import PathManager
import os


class Checkpointer(fvCheckpointer):
    def load(self, path: str, checkpointables: Optional[List[str]] = None) -> object:
        """
        Load from the given checkpoint. When path points to network file, this
        function has to be called on all ranks.
        Args:
            path (str): path or url to the checkpoint. If empty, will not load
                anything.
            checkpointables (list): List of checkpointable names to load. If not
                specified (None), will load all the possible checkpointables.
        Returns:
            dict:
                extra data loaded from the checkpoint that has not been
                processed. For example, those saved with
                :meth:`.save(**extra_data)`.
        """
        if not path:
            # no checkpoint provided
            self.logger.info("No checkpoint found. Initializing model from scratch")
            return {}
        self.logger.info("Loading checkpoint from {}".format(path))
        if not os.path.isfile(path):
            path = PathManager.get_local_path(path)
            assert os.path.isfile(path), "Checkpoint {} not found!".format(path)

        checkpoint = self._load_file(path)
        incompatible = self._load_model(checkpoint)
        if (
            incompatible is not None
        ):  # handle some existing subclasses that returns None
            self._log_incompatible_keys(incompatible)

        for key in self.checkpointables if checkpointables is None else checkpointables:
            if key in checkpoint:  # pyre-ignore
                self.logger.info("Loading {} from {}".format(key, path))
                obj = self.checkpointables[key]
                try:
                    obj.load_state_dict(checkpoint.pop(key))  # pyre-ignore
                except:
                    print("exception loading")

        # return any further checkpoint data
        return checkpoint
