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

import logging
import unittest
import unittest.mock
import pytest
import numpy as np
import torch
import torch.nn
import torch.nn.functional as functional

from aimet_common.utils import AimetLogger
from aimet_torch.utils import to_numpy, create_fake_data_loader, compute_encoding_for_given_bitwidth,\
    create_encoding_from_dict
from aimet_torch.quantsim import QuantizationSimModel
from aimet_torch.qc_quantize_op import StaticGridQuantWrapper, QuantScheme, MAP_QUANT_SCHEME_TO_PYMO
from aimet_torch.examples.test_models import TinyModel
from aimet_torch.adaround.adaround_weight import Adaround
from aimet_torch.adaround.adaround_loss import AdaroundLoss
from aimet_torch.adaround.adaround_optimizer import AdaroundOptimizer
from aimet_torch.adaround.adaround_loss import AdaroundHyperParameters
from aimet_torch.tensor_quantizer import QuantizationDataType

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Test)


class TestAdaroundOptimizer(unittest.TestCase):

    def _optimize_layer_rounding(self, warm_start):

        AimetLogger.set_level_for_all_areas(logging.DEBUG)
        model = TinyModel().eval()
        dummy_input = torch.randn(1, 3, 32, 32)
        sim = QuantizationSimModel(model, dummy_input=dummy_input, quant_scheme='tf_enhanced', default_param_bw=4)

        module = model.conv1
        quant_module = sim.model.conv1

        nearest_encoding = unittest.mock.MagicMock()
        nearest_encoding.bw = 4
        nearest_encoding.offset = -127.0
        nearest_encoding.delta = 0.001551126479
        quant_module.param_quantizers['weight'].encoding = nearest_encoding

        Adaround._replace_tensor_quantizer(quant_module)
        alpha = torch.randn(quant_module._module_to_wrap.weight.shape, requires_grad=True)
        quant_module.param_quantizers['weight'].alpha = alpha

        # create copy of parameter
        before_opt = alpha.clone()

        dataset_size = 50
        batch_size = 10
        image_size = (3, 32, 32)
        data_loader = create_fake_data_loader(dataset_size, batch_size, image_size)
        opt_params = AdaroundHyperParameters(num_iterations=10, reg_param=0.01, beta_range=(20, 2),
                                             warm_start=warm_start)

        AdaroundOptimizer.adaround_module(module, quant_module, model, sim.model, None, data_loader, opt_params)

        after_opt = quant_module.param_quantizers['weight'].alpha

        # parameter should be different before and after optimization
        self.assertFalse(np.array_equal(to_numpy(before_opt), to_numpy(after_opt)))

        # alpha's gradient should not be None
        self.assertTrue(quant_module.param_quantizers['weight'].alpha.grad is not None)

    def test_optimize_rounding_with_only_recons_loss(self):
        """ test optimize layer rounding with reconstruction loss """
        # warm_start = 1.0 forces rounding loss to be zero
        warm_start = 1.0
        self._optimize_layer_rounding(warm_start)

    def test_optimize_rounding_with_combined_loss(self):
        """ test optimize layer rounding with combined loss """
        warm_start = 0.2
        self._optimize_layer_rounding(warm_start)

    def test_split_val_into_chunks(self):
        """ Test split value logic """
        splits = AdaroundOptimizer._split_into_chunks(10, 2)
        self.assertEqual(splits, [5, 5])
        splits = AdaroundOptimizer._split_into_chunks(32, 3)
        self.assertEqual(splits, [10, 11, 11])
        splits = AdaroundOptimizer._split_into_chunks(10000, 3)
        self.assertEqual(splits, [3333, 3333, 3334])

    def test_compute_recons_metrics(self):
        """ Test compute reconstruction metrics function """
        np.random.seed(0)
        torch.manual_seed(0)
        quant_scheme = QuantScheme.post_training_tf_enhanced
        weight_bw = 8
        activation_bw = 8

        weight_data = np.random.rand(4, 4, 1, 1).astype(dtype='float32')
        encoding_dict = compute_encoding_for_given_bitwidth(weight_data, weight_bw,
                                                            MAP_QUANT_SCHEME_TO_PYMO[quant_scheme], False,
                                                            QuantizationDataType.int)
        encoding, _ = create_encoding_from_dict(encoding_dict)

        print(encoding_dict['scale'], encoding_dict['max'])
        self.assertAlmostEqual(encoding_dict['scale'], 0.003772232448682189, places=3)

        conv1 = torch.nn.Conv2d(4, 4, 1, bias=False)
        conv1.weight.data = torch.from_numpy(weight_data)
        quant_module = StaticGridQuantWrapper(conv1, weight_bw, activation_bw, round_mode='nearest',
                                              quant_scheme=quant_scheme)

        quant_module.param_quantizers['weight'].encoding = encoding
        Adaround._replace_tensor_quantizer(quant_module)

        inp_data = np.random.rand(1, 4, 10, 10).astype(dtype='float32')
        inp_data = torch.from_numpy(inp_data)
        out_data = np.random.rand(1, 4, 10, 10).astype(dtype='float32')
        out_data = torch.from_numpy(out_data)

        recons_err_hard, recons_err_soft = AdaroundOptimizer._compute_recons_metrics(quant_module, None, inp_data,
                                                                                     out_data)

        print(recons_err_hard, recons_err_soft)
        self.assertAlmostEqual(recons_err_hard, 0.610206663608551, places=3)
        self.assertAlmostEqual(recons_err_soft, 0.6107949018478394, places=3)

    @pytest.mark.cuda
    def test_compute_output_with_adarounded_weights(self):
        """ Test compute output with adarounded weights for Conv layer """
        np.random.seed(0)
        torch.manual_seed(0)
        quant_scheme = QuantScheme.post_training_tf_enhanced
        weight_bw = 8
        activation_bw = 8

        weight_data = np.random.rand(4, 4, 1, 1).astype(dtype='float32')

        conv1 = torch.nn.Conv2d(4, 4, 1, bias=False).to(torch.device('cuda'))
        conv1.weight.data = torch.from_numpy(weight_data).to(torch.device('cuda'))
        quant_module = StaticGridQuantWrapper(conv1, weight_bw, activation_bw, round_mode='nearest',
                                              quant_scheme=quant_scheme)

        # Compute encodings
        quant_module.param_quantizers['weight'].update_encoding_stats(conv1.weight.data)
        quant_module.param_quantizers['weight'].compute_encoding()
        encoding = quant_module.param_quantizers['weight'].encoding
        print(encoding.max, encoding.min)

        # Replace the quantizer
        Adaround._replace_tensor_quantizer(quant_module)

        inp_data = np.random.rand(1, 4, 10, 10).astype(dtype='float32')
        inp_data = torch.from_numpy(inp_data).to(torch.device('cuda'))
        out_data = np.random.rand(1, 4, 10, 10).astype(dtype='float32')
        out_tensor = torch.from_numpy(out_data).to(torch.device('cuda'))

        adaround_out_tensor = AdaroundOptimizer._compute_output_with_adarounded_weights(quant_module, inp_data)

        # Compute mse loss
        mse_loss = functional.mse_loss(adaround_out_tensor, out_tensor)
        print(mse_loss.detach().cpu().numpy())
        self.assertAlmostEqual(mse_loss.detach().cpu().numpy(), 0.6107949, places=2)

        # Compute adaround reconstruction loss (squared Fro norm)
        recon_loss = AdaroundLoss.compute_recon_loss(adaround_out_tensor, out_tensor)
        print(recon_loss.detach().cpu().numpy())
        self.assertAlmostEqual(recon_loss.detach().cpu().numpy(), 2.4431798, places=2)
