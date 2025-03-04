# /usr/bin/env python3.6
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2021, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================

"""
This file demonstrates the use of Range Learning using AIMET APIs.
"""

import os
import argparse
from datetime import datetime
import logging
from typing import List, Callable, Any
import tensorflow as tf
from tensorflow.python.keras.applications.resnet import ResNet50

# imports for AIMET
from aimet_common.defs import QuantScheme
from aimet_tensorflow import batch_norm_fold as aimet_bnf
from aimet_tensorflow import cross_layer_equalization as aimet_cle
from aimet_tensorflow.quantsim import QuantizationSimModel
from aimet_tensorflow.utils.graph_saver import save_and_load_graph
from aimet_tensorflow.utils.graph_saver import save_model_to_meta

# imports for data pipelines
from Examples.common import image_net_config
from Examples.tensorflow.utils.image_net_trainer import ImageNetTrainer
from Examples.tensorflow.utils.image_net_evaluator import ImageNetEvaluator
from Examples.tensorflow.utils.add_computational_nodes_in_graph import add_image_net_computational_nodes_in_graph

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)

logger = logging.getLogger('TensorFlowRangeLearning')
formatter = logging.Formatter('%(asctime)s : %(name)s - %(levelname)s - %(message)s')
logging.basicConfig(format=formatter)


###
# This script utilizes AIMET to perform Range Learning on a resnet50 pretrained model
# with the ImageNet data set. This is intended as a working example to show how AIMET
# APIs can be invoked.

# Scenario parameters:
#    - AIMET Range Learning using simulation model
#    - Quant Scheme: QuantScheme.training_range_learning_with_tf_init
#    - rounding_mode: 'nearest'
#    - default_output_bw: 8, default_param_bw: 8
#	 - Encoding computation using 5 batches of data
#    - Input shape: [1, 3, 224, 224]
#    - Learning rate: 0.001
#    - Decay Steps: 5
###


class ImageNetDataPipeline:
    """
    Provides APIs for data-loader and model evaluation using ImageNet TFRecords.
    """

    def __init__(self, _config: argparse.Namespace):
        """
        Instantiates ImageNetDataPipeline object

        :param _config:
        """
        self._config = _config

    def evaluate(self, sess: tf.Session, iterations: int = None) -> float:
        """
        Evaluate the specified session using the specified number of samples from the validation set.
        AIMET's QuantizationSimModel.compute_encodings() expects the function with this signature
        to its eval_callback parameter.

        :param sess: The sess graph to be evaluated.
        :param iterations: The number of batches of the dataset.
        :return: The accuracy for the sample with the maximum accuracy.
        """

        # your code goes here instead of the example from below

        evaluator = ImageNetEvaluator(self._config.tfrecord_dir, training_inputs=['keras_learning_phase:0'],
                                      data_inputs=['input_1:0'], validation_inputs=['labels:0'],
                                      image_size=image_net_config.dataset['image_size'],
                                      batch_size=image_net_config.evaluation['batch_size'],
                                      format_bgr=False, model_type='mobilenet')

        return evaluator.evaluate(sess, iterations)

    def finetune(self, sess: tf.Session, update_ops_name: List[str] = None):
        """
        Fine-tunes the session graph. The implementation provided here is just an example,
        provide your own implementation if needed.

        :param sess: The sess graph to fine-tune.
        :param update_ops_name: list of name of update ops (mostly BatchNorms' moving averages).
                                tf.GraphKeys.UPDATE_OPS collections is always used
                                in addition to this list
        """

        # Your code goes here instead of the example from below

        trainer = ImageNetTrainer(self._config.tfrecord_dir, training_inputs=['keras_learning_phase:0'],
                                  data_inputs=['input_1:0'], validation_inputs=['labels:0'],
                                  image_size=image_net_config.dataset['image_size'],
                                  batch_size=image_net_config.train['batch_size'],
                                  num_epochs=self._config.epochs, format_bgr=True)

        trainer.train(sess, update_ops_name=update_ops_name, learning_rate=self._config.learning_rate,
                      decay_steps=self._config.decay_steps)

        save_model_to_meta(sess, meta_path=os.path.join(self._config.logdir, 'finetuned_model'))


def create_quant_sim_model(sess: tf.Session, start_op_names: List[str], output_op_names: List[str],
                           use_cuda: bool, evaluator: Callable[[tf.Session, Any], None],
                           logdir: str) -> QuantizationSimModel:
    """
    Apply quantizer simulator on the original model and return its object.

    :param sess: The sess with graph.
    :param start_op_names: The list of input op names of the sess.graph
    :param output_op_names: The list of output op names of the sess.graph
    :param use_cuda: If True then use a GPU for QuantizationSimModel
    :param evaluator: A callback function that is expected to run forward passes on a session
    :param logdir: Path to a directory for logging, required by save_and_load_graph API
    :return: QuantizationSimModel object
    """

    # Since QuantizationSimModel operates on a session inplace, making a
    # copy of original session and use it for further processing to keep
    # original session intact.
    copied_sess = save_and_load_graph(sess=sess, meta_path=logdir)

    quant_scheme = QuantScheme.training_range_learning_with_tf_enhanced_init

    # Rounding mode can be 'nearest' or 'stochastic'
    rounding_mode = 'nearest'

    # Output bitwidth for quantization
    default_output_bw = 8

    # Parameter bitwidth for quantization
    default_param_bw = 8

    quant_sim_model = QuantizationSimModel(session=copied_sess,
                                           starting_op_names=start_op_names,
                                           output_op_names=output_op_names,
                                           quant_scheme=quant_scheme, rounding_mode=rounding_mode,
                                           default_output_bw=default_output_bw,
                                           default_param_bw=default_param_bw,
                                           use_cuda=use_cuda)

    # Number of batches to use for computing encodings
    # Only 5 batches are used here to speed up the process, also the
    # number of images in these 5 batches should be sufficient for
    # compute encodings
    iterations = 5

    # Here evaluator is used for forward_pass_callback as it is available
    # from Data Pipeline class. But any forward pass function can be used
    # here which doesn't necessarily need to use any labels data or return
    # any output. For Example, following snippet of code can be used for
    # forward_pass_callback:

    # def forward_pass_callback(session: tf.Session, iterations: int):
    #     input_tensor = <input tensor in session>
    #     train_tensor = <train tensor in session>

    #     curr_iter = 1
    #     for input_data, _ in data_loaders:
    #         feed_dict = {input_tensor: input_data,
    #                      train_tensor: False}

    #         session.run([], feed_dict=feed_dict)

    #         curr_iter += 1
    #         if curr_iter > iterations:
    #             break

    quant_sim_model.compute_encodings(forward_pass_callback=evaluator,
                                      forward_pass_callback_args=iterations)

    return quant_sim_model

def aimet_cross_layer_equalization(sess: tf.Session, start_op_names: List[str], output_op_names: List[str]) -> tf.Session:
    """
    Applies CLE on the model which consists of:
        Batch Norm Folding
        Cross Layer Scaling
        High Bias Fold
    Also converts any ReLU6 into ReLU.

    :param sess: The sess with graph to be evaluated
    :param start_op_names: The list of input op names of the sess.graph
    :param output_op_names: The list of output op names of the sess.graph
    :return: The sess with graph after CLE applied
    """

    # Applying Cross Layer Equalization
    cle_applied_sess = aimet_cle.equalize_model(sess,
                                                start_op_names=start_op_names,
                                                output_op_names=output_op_names)

    return cle_applied_sess


def aimet_range_learning(config: argparse.Namespace):
    """
    1. Instantiates Data Pipeline for evaluation
    2. Loads the pretrained resnet50 keras model
    3. Calculates Model accuracy
        3.1. Calculates floating point accuracy
        3.2. Calculates Quant Simulator accuracy
    4. Applies AIMET CLE (optional)
        4.1. Applies AIMET CLE and calculates QuantSim accuracy
    5. Quantization Aware Training
        5.1. Trains the quantization aware model
        5.2. Calculates and logs the accuracy of quantization Aware trained model
        5.3. Exports quantization aware trained (QAT) model so it is ready to be run on-target

    :param config: This argparse.Namespace config expects following parameters:
                   tfrecord_dir: Path to a directory containing ImageNet TFRecords.
                                This folder should contain files starting with:
                                'train*': for training records and 'validation*': for validation records
                   use_cuda: A boolean var to indicate to run the test on GPU.
                   logdir: Path to a directory for logging.
                   epochs: Number of epochs (type int) for training.
                   learning_rate: A float type learning rate for model training
                   decay_steps: A number used to adjust(decay) the learning rate after every decay_steps
                                epochs in training.
    """

    # 1. Instantiates Data Pipeline for evaluation and training
    data_pipeline = ImageNetDataPipeline(config)

    # 2. Loads the pretrained resnet50 keras model
    input_shape = (image_net_config.dataset['image_width'],
                   image_net_config.dataset['image_height'],
                   image_net_config.dataset['image_channels'])
    tf.keras.backend.clear_session()
    model = ResNet50(weights='imagenet', input_shape=input_shape)
    sess = tf.keras.backend.get_session()
    add_image_net_computational_nodes_in_graph(sess, model.output, image_net_config.dataset['images_classes'])
    update_ops_name = [op.name for op in model.updates]

    # 3. Calculates Model accuracy

    # 3.1. Floating Point accuracy
    accuracy = data_pipeline.evaluate(sess)
    logger.info("Original Model Top-1 accuracy = %.2f", accuracy)

    # 3.2. Quant Simulator accuracy

    # It is recommended to fold Batch-norms before running on target.
    # But CLE needs original model/graph to operate upon, so making a
    # copy of model/graph here, before folding BN layers.
    BN_folded_sess = save_and_load_graph(sess=sess, meta_path=config.logdir)
    BN_folded_sess, _ = aimet_bnf.fold_all_batch_norms(BN_folded_sess, input_op_names=['input_1'],
                                                       output_op_names=[model.output.name.split(":")[0]])

    # Creating QuantSim model
    quant_sim = create_quant_sim_model(BN_folded_sess, start_op_names=['input_1'],
                                       output_op_names=[model.output.name.split(":")[0]],
                                       use_cuda=config.use_cuda, evaluator=data_pipeline.evaluate,
                                       logdir=config.logdir)
    # Calculating QuantSim model accuracy
    accuracy = data_pipeline.evaluate(quant_sim.session)
    logger.info("Original Model Top-1 accuracy on Quant Simulator = %.2f", accuracy)

    # 4. Applies AIMET CLE
    logger.info("Starting Aimet Cross Layer Equalization")

    # 4.1. Applies AIMET CLE and calculates QuantSim accuracy
    # Note: Applying CLE is an optional step for range_learning
    cle_applied_sess = aimet_cross_layer_equalization(sess, start_op_names=['input_1'],
                                                      output_op_names=[model.output.name.split(":")[0]])

    # Creating QuantSim model
    quant_sim.session.close()
    quant_sim = create_quant_sim_model(cle_applied_sess, start_op_names=['input_1'],
                                       output_op_names=[model.output.name.split(":")[0]],
                                       use_cuda=config.use_cuda, evaluator=data_pipeline.evaluate,
                                       logdir=config.logdir)
    # Calculating QuantSim model accuracy
    accuracy = data_pipeline.evaluate(quant_sim.session)
    logger.info("CLE Applied Model Top-1 accuracy on Quant Simulator = %.2f", accuracy)
    logger.info("Model Quantization Complete")

    # 5. Quantization Aware Training (QAT)
    logger.info("Starting Model QAT")

    # 5.1. Trains the quantization aware model
    data_pipeline.finetune(quant_sim.session, update_ops_name=update_ops_name)

    # 5.2. Calculates and logs the accuracy of Quantization Aware Trained model
    accuracy = data_pipeline.evaluate(quant_sim.session)
    logger.info("Applied Range Learning, Top-1 Accuracy on Quant Simulator = %.2f", accuracy)

    # 5.3 Exports quantization aware trained model so it is ready to be run on-target
    logger.info("Saving Quantized model graph")
    quant_sim.export(path=config.logdir, filename_prefix='quantized_model')
    logger.info("Quantized model graph is saved!")
    logger.info("Aimet Range Learning Complete")

if __name__ == '__main__':
    default_logdir = os.path.join("benchmark_output", "range_learning_" + datetime.now().strftime("%Y-%m-%d-%H-%M-%S"))

    parser = argparse.ArgumentParser(
        description='Perform Range Learning on pretrained Resnet50 model for ImageNet dataset to \
                                                  show improvement of its accuracy on Quantized platform.')

    parser.add_argument('--tfrecord_dir', type=str,
                        required=True,
                        help="Path to a directory containing ImageNet TFRecords.\n\
                              This folder should contain files starting with:\n\
                              'train*': for training records and 'validation*': for validation records")
    parser.add_argument('--use_cuda', action='store_true',
                        default=False,
                        help='Add this flag to run the test on GPU.')

    parser.add_argument('--logdir', type=str,
                        default=default_logdir,
                        help="Path to a directory for logging.\
                              Default value is 'benchmark_output/cle_bc_<Y-m-d-H-M-S>'")
    parser.add_argument('--epochs', type=int,
                        default=15,
                        help="Number of epochs for finetuning.\n\
                              Default is 15")
    parser.add_argument('--learning_rate', type=float,
                        default=1e-3,
                        help="A float type learning rate for model finetuning.\n\
                              default is 0.001")
    parser.add_argument('--decay_steps', type=int,
                        default=5,
                        help="A number used to adjust(decay) the learning rate after every decay_steps epochs in finetuning.\n\
                              default is 5")


    _config = parser.parse_args()

    os.makedirs(_config.logdir, exist_ok=True)

    fileHandler = logging.FileHandler(os.path.join(_config.logdir, "test.log"))
    fileHandler.setFormatter(formatter)
    logger.addHandler(fileHandler)

    aimet_range_learning(_config)
