import logging
import sys
import naslib as nl

from naslib.defaults.predictor_evaluator import PredictorEvaluator
from naslib.defaults.trainer import Trainer
from naslib.optimizers import Bananas, OneShotNASOptimizer, RandomNASOptimizer
from naslib.predictors import OneShotPredictor

from naslib.search_spaces import NasBench101SearchSpace, NasBench201SearchSpace, DartsSearchSpace
from naslib.utils import utils, setup_logger, get_dataset_api
from naslib.utils.utils import get_project_root


config = utils.get_config_from_args(config_type='nas_predictor')

utils.set_seed(config.seed)
logger = setup_logger(config.save + "/log.log")
logger.setLevel(logging.INFO)

utils.log_args(config)

supported_optimizers = {
    'bananas': Bananas(config),
    'oneshot': OneShotNASOptimizer(config),
    'rsws': RandomNASOptimizer(config),
}

supported_search_spaces = {
    'nasbench101': NasBench101SearchSpace(),
    'nasbench201': NasBench201SearchSpace(),
    'darts': DartsSearchSpace()
}


load_labeled = (True if config.search_space == 'darts' else False)
dataset_api = get_dataset_api(config.search_space, config.dataset)

search_space = supported_search_spaces[config.search_space]

optimizer = supported_optimizers[config.optimizer]
optimizer.adapt_search_space(search_space, dataset_api=dataset_api)

trainer = Trainer(optimizer, config, lightweight_output=True)
#trainer.evaluate(resume_from=utils.get_last_checkpoint(config, search=False) if config.resume else "")

if config.optimizer == 'bananas':
    trainer.search(resume_from="")
    trainer.evaluate(resume_from="", dataset_api=dataset_api)
elif config.optimizer in ['oneshot', 'rsws']:
    predictor = OneShotPredictor(config, trainer,
                                 encoding_type='adjacency_one_hot',
                                 model_path=config.resume_from)
    predictor_evaluator = PredictorEvaluator(predictor, config=config)
    predictor_evaluator.adapt_search_space(search_space, load_labeled=load_labeled, dataset_api=dataset_api)

    # evaluate the predictor
    predictor_evaluator.evaluate()

