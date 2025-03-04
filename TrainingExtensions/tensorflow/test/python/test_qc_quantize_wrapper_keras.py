# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2022, Qualcomm Innovation Center, Inc. All rights reserved.
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
import tensorflow as tf
import numpy as np
from packaging import version

from aimet_tensorflow.keras.quant_sim.qc_quantize_wrapper import QcQuantizeWrapper, QuantizerSettings
import libpymo

def dense_functional():
    inp = tf.keras.layers.Input(shape=(5,))
    x = tf.keras.layers.Dense(units=2)(inp)
    x = tf.keras.layers.Softmax()(x)
    model = tf.keras.Model(inputs=inp, outputs=x, name="dense_functional")
    return model

def dense_sequential():
    model = tf.keras.Sequential()
    model.add(tf.keras.layers.Dense(units=2, input_shape=(5,)))
    model.add(tf.keras.layers.Softmax())
    return model

class DenseSubclassing(tf.keras.Model):
    def __init__(self):
        super(DenseSubclassing, self).__init__()
        self.linear1 = tf.keras.layers.Dense(units=2)
        self.softmax = tf.keras.layers.Softmax()

    def call(self, inputs, training=None, mask=None):
        x = self.linear1(inputs)
        x = self.softmax(x)
        return x

def test_wrapper():
    if version.parse(tf.version.VERSION) >= version.parse("2.00"):
        test_inp = np.array([[1.5, 2.5]])
        inp = tf.keras.layers.Input(shape=(2,))
        dense = tf.keras.layers.Dense(3, kernel_initializer=tf.initializers.Constant([[2.3,-1.4, .5], [-.6, 3.1, -.2]]),
                                      bias_initializer=tf.initializers.Constant([5.0]))
        # run forward pass on dense to generate weights
        _ = dense(test_inp)
        x = QcQuantizeWrapper(dense,
                              QuantizerSettings(8, 'nearest', 'tf', False, False, False),
                              QuantizerSettings(8, 'nearest', 'tf', False, False, False))(inp)
        model = tf.keras.Model(inputs=inp, outputs=x)

        _ = model.predict(test_inp)

        # Disable input quantizer and check later that quantizer mode remains passThrough, and encoding is None
        model.layers[1].input_quantizers[0].disable()
        model.layers[1].param_quantizers[1].disable()
        model.layers[1].compute_encoding()
        assert model.layers[1].input_quantizers[0].quant_mode == 3
        assert model.layers[1].output_quantizers[0].quant_mode == 2
        assert model.layers[1].input_quantizers[0].encoding is None
        assert model.layers[1].output_quantizers[0].encoding is not None

        model.layers[1].output_quantizers[0].disable()
        param_quant_only = model.predict(test_inp)
        model.layers[1].output_quantizers[0].enable()
        param_and_output_quant = model.predict(test_inp)
        assert np.allclose(param_quant_only, np.array([[6.9411764145, 10.6735286713,  5.2558822632]]))
        assert np.allclose(param_and_output_quant, np.array([[6.9482579231, 10.6735286713,  5.2739787102]]))

        model.compile(optimizer=tf.keras.optimizers.Adam(learning_rate=1e-3),
                           loss=tf.keras.losses.MeanSquaredError())
        test_out = np.random.randn(1, 3)
        for i in range(10):
            starting_weights = [tf.keras.backend.get_value(param) for param in model.layers[1]._layer_to_wrap.weights]
            model.fit(x=test_inp, y=test_out, batch_size=1)
            weights_after_fit = [tf.keras.backend.get_value(param) for param in model.layers[1]._layer_to_wrap.weights]
            for idx, weight in enumerate(starting_weights):
                assert not np.array_equal(weight, weights_after_fit[idx])
            _ = model.predict(test_inp)
            weights_after_predict = [tf.keras.backend.get_value(param) for param in
                                     model.layers[1]._layer_to_wrap.weights]
            for idx, weight in enumerate(weights_after_predict):
                assert np.array_equal(weight, weights_after_fit[idx])

def test_wrapper_settings():
    if version.parse(tf.version.VERSION) >= version.parse("2.00"):
        test_inp = np.array([[-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0 ,7.0, 8.0]])
        inp = tf.keras.layers.Input(shape=(12,))
        identity = tf.keras.layers.Lambda(lambda x: x)
        out = QcQuantizeWrapper(identity,
                              QuantizerSettings(8, 'nearest', 'tf_enhanced', False, False, False),
                              QuantizerSettings(8, 'nearest', 'tf_enhanced', False, False, False))(inp)
        model = tf.keras.Model(inputs=inp, outputs=out)

        _ = model.predict(test_inp)
        model.layers[1].input_quantizers[0].compute_encoding()
        quant_out_0 = model.predict(test_inp)

        model.layers[1].input_quantizers[0].is_symmetric = True
        model.layers[1].input_quantizers[0].use_strict_symmetric = True
        _ = model.predict(test_inp)
        model.layers[1].input_quantizers[0].compute_encoding()
        assert model.layers[1].input_quantizers[0].encoding.offset == -127  # Test strict symmetric
        quant_out_1 = model.predict(test_inp)
        assert quant_out_0[0][0] != quant_out_1[0][0]   # Test that changed settings take effect

        model.layers[1].input_quantizers[0].quant_scheme = libpymo.QuantizationMode.QUANTIZATION_TF_ENHANCED
        model.layers[1].input_quantizers[0].round_mode = libpymo.RoundingMode.ROUND_STOCHASTIC
        model.layers[1].input_quantizers[0].bitwidth = 2
        model.layers[1].input_quantizers[0].is_symmetric = False
        model.layers[1].input_quantizers[0].use_strict_symmetric = False

        _ = model.predict(test_inp)
        model.layers[1].input_quantizers[0].compute_encoding()
        quant_out = model.predict(test_inp)

        # Check that by changing bitwidth to 2, the number of distinct quant/dequant values in the output is 4
        out_values = set()
        for num in quant_out[0]:
            out_values.add(num)
        assert len(out_values) == 4
