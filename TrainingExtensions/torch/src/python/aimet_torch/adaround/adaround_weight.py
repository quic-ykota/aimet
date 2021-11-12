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

""" Top level API for Adaptive Rounding - Post-Training Quantization (PTQ) """

import os
import json
import shutil
from typing import Tuple, Union, Dict, List
import torch
from torch.utils.data import DataLoader

# Import AIMET specific modules
from aimet_common.utils import AimetLogger
from aimet_common.defs import QuantScheme

from aimet_torch import utils
from aimet_torch.save_utils import SaveUtils
from aimet_torch.meta import connectedgraph_utils
from aimet_torch.quantsim import QuantizationSimModel, QcQuantizeWrapper
from aimet_torch.qc_quantize_op import StaticGridQuantWrapper, QcQuantizeOpMode
from aimet_torch.adaround.adaround_tensor_quantizer import AdaroundTensorQuantizer
from aimet_torch.adaround.adaround_optimizer import AdaroundOptimizer
from aimet_torch.adaround.adaround_loss import AdaroundHyperParameters
from aimet_torch.tensor_quantizer import QuantizationDataType

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)

# The following modules with weights are supported by Adaround
AdaroundSupportedModules = (torch.nn.Conv2d, torch.nn.ConvTranspose2d, torch.nn.Linear)
WORKING_DIR = '/tmp/adaround/'


class AdaroundParameters:
    """
    Configuration parameters for Adaround
    """
    def __init__(self, data_loader: DataLoader, num_batches: int,
                 default_num_iterations: int = 10000, default_reg_param: float = 0.01,
                 default_beta_range: Tuple = (20, 2), default_warm_start: float = 0.2):
        """
        :param data_loader: Data loader
        :param num_batches: Number of batches
        :param default_num_iterations: Number of iterations to adaround each layer. Default 10000
        :param default_reg_param: Regularization parameter, trading off between rounding loss vs reconstruction loss.
         Default 0.01
        :param default_beta_range: Start and stop beta parameter for annealing of rounding loss (start_beta, end_beta).
         Default (20, 2)
        :param default_warm_start: warm up period, during which rounding loss has zero effect. Default 20% (0.2)
        """
        self.data_loader = data_loader
        self.num_batches = num_batches
        self.num_iterations = default_num_iterations
        self.reg_param = default_reg_param
        self.beta_range = default_beta_range
        self.warm_start = default_warm_start


class Adaround:
    """
    Weight-rounding mechanism for Post Training Quantization (PTQ)
    """
    @classmethod
    def apply_adaround(cls, model: torch.nn.Module, dummy_input: Union[torch.Tensor, Tuple], params: AdaroundParameters,
                       path: str, filename_prefix: str, default_param_bw: int = 4,
                       param_bw_override_list: List[Tuple[torch.nn.Module, int]] = None,
                       ignore_quant_ops_list: List[torch.nn.Module] = None,
                       default_quant_scheme: QuantScheme = QuantScheme.post_training_tf_enhanced,
                       default_config_file: str = None) -> torch.nn.Module:
        """
        Returns model with optimized weight rounding of every module (Conv and Linear) and also saves the
        corresponding quantization encodings to a separate JSON-formatted file that can then be imported by
        QuantSim for inference or QAT

        :param model: Model to Adaround
        :param dummy_input: Dummy input to the model. Used to parse model graph. If the model has more than one input,
                            pass a tuple. User is expected to place the tensors on the appropriate device.
        :param params: Parameters for Adaround
        :param path: path where to store parameter encodings
        :param filename_prefix: Prefix to use for filename of the encodings file
        :param default_param_bw: Default bitwidth (4-31) to use for quantizing layer parameters
        :param param_bw_override_list: List of Tuples. Each Tuple is a module and the corresponding parameter bitwidth
                                       to be used for that module.
        :param ignore_quant_ops_list: Ops listed here are skipped during quantization needed for AdaRounding. Do not
                                      specify Conv and Linear modules in this list. Doing so, will affect accuracy.
        :param default_quant_scheme: Quantization scheme. Supported options are using Quant Scheme Enum
                                    QuantScheme.post_training_tf or QuantScheme.post_training_tf_enhanced
        :param default_config_file: Default configuration file for model quantizers
        :return: Model with Adarounded weights and saves corresponding parameter encodings JSON file at provided path
        """
        # pylint: disable=too-many-arguments
        # Create Quant sim with given parameters
        quant_sim = QuantizationSimModel(model, dummy_input=dummy_input, quant_scheme=default_quant_scheme,
                                         default_param_bw=default_param_bw,
                                         config_file=default_config_file)

        # For the modules in the param_bw_override_list, override the default parameter bitwidths in the QuantSim
        if param_bw_override_list:
            cls._override_param_bitwidth(model, quant_sim, param_bw_override_list)

        if ignore_quant_ops_list:
            cls._skip_quantization_for_ops(model, quant_sim, ignore_quant_ops_list)

        # Compute only param encodings
        cls._compute_param_encodings(quant_sim)

        # Get the module - activation function pair using ConnectedGraph
        module_act_func_pair = connectedgraph_utils.get_module_act_func_pair(model, dummy_input)

        cls._adaround_model(model, quant_sim, module_act_func_pair, params, dummy_input)

        # Update every module (AdaroundSupportedModules) weight with Adarounded weight (Soft rounding)
        cls._update_modules_with_adarounded_weights(quant_sim)

        # Export quantization encodings to JSON-formatted file
        cls._export_encodings_to_json(path, filename_prefix, quant_sim)

        SaveUtils.remove_quantization_wrappers(quant_sim.model)
        logger.info('Completed Adarounding Model')

        return quant_sim.model

    @classmethod
    def _adaround_model(cls, model: torch.nn.Module, quant_sim: QuantizationSimModel, module_act_func_pair: Dict,
                        params: AdaroundParameters, dummy_input: Union[torch.Tensor, Tuple]):
        """
        Optimize weight rounding of every module (AdaroundSupportedModules) of model in sequential manner
        based on occurrence
        :param model: The original, un quantized, model
        :param quant_sim: Quant sim
        :param module_act_func_pair: Dictionary of module to immediate following activation function
        :param params: Adaround parameters
        :param dummy_input: Dummy input to the model
        """
        # Cache model input data to WORKING_DIR
        cached_dataset = utils.CachedDataset(params.data_loader, params.num_batches, WORKING_DIR)

        # Optimization Hyper parameters
        opt_params = AdaroundHyperParameters(params.num_iterations, params.reg_param, params.beta_range,
                                             params.warm_start)

        # AdaRound must be applied to modules in the order of occurrence
        for name, module in utils.get_ordered_list_of_modules(model, dummy_input):
            if isinstance(module, AdaroundSupportedModules):

                # Using name, get corresponding quantized wrapper module from Quant sim model
                quant_module = cls._get_quant_module(quant_sim.model, name)

                # Replace quant module's tensor quantizer with Adaround tensor quantizer
                cls._replace_tensor_quantizer(quant_module)

                # Get module's next following activation function
                act_func = module_act_func_pair[module]

                logger.info("Started Optimizing weight rounding of module: %s", name)
                AdaroundOptimizer.adaround_module(module, quant_module, model, quant_sim.model, act_func,
                                                  cached_dataset, opt_params)

        if os.path.exists(WORKING_DIR):
            logger.info('Deleting model inputs from location: %s', WORKING_DIR)
            shutil.rmtree(WORKING_DIR)

    @staticmethod
    def _compute_param_encodings(quant_sim: QuantizationSimModel):
        """
        Compute encodings for parameters, needed for initializing Adaround quantizers
        :param quant_sim: Quant sim
        """
        for quant_module in quant_sim.model.modules():
            if isinstance(quant_module, StaticGridQuantWrapper):
                # Adaround requires input and output quantizers to be disabled
                quant_module.input_quantizer.enabled = False
                quant_module.output_quantizer.enabled = False

                # pylint: disable=protected-access
                for name, param in quant_module._module_to_wrap.named_parameters():
                    param_quantizer = quant_module.param_quantizers[name]
                    param_quantizer.reset_encoding_stats()
                    param_quantizer.update_encoding_stats(param.data)
                    param_quantizer.compute_encoding()

                # Wrapper mode must be set to ACTIVE because the wrapper's quantize_dequantize_params() will only call
                # into the param tensor quantizer's quantize_dequantize() if the mode is not PASSTHROUGH.
                quant_module.set_mode(QcQuantizeOpMode.ACTIVE)

    @staticmethod
    def _replace_tensor_quantizer(quant_module: StaticGridQuantWrapper):
        """
        Replace the quantized module's weight tensor quantizer with the Adaround tensor quantizer
        :param quant_module: quant module
        """
        assert quant_module.param_quantizers['weight'], '%s does not have weight parameter.' % quant_module
        assert quant_module.param_quantizers['weight'].encoding, '%s encoding needs to be set.' % quant_module

        quantizer = quant_module.param_quantizers['weight']
        adaround_quantizer = AdaroundTensorQuantizer(quantizer.bitwidth, 'Adaptive', quantizer.quant_scheme,
                                                     quantizer.use_symmetric_encodings, quantizer.enabled)

        # Set the encodings and replace by Adaround tensor quantizer
        adaround_quantizer.encoding = quantizer.encoding
        quant_module.param_quantizers['weight'] = adaround_quantizer

    @staticmethod
    def _get_quant_module(quant_sim_model: torch.nn.Module, module_name: str) -> Union[StaticGridQuantWrapper, None]:
        """
        For given module name, get the quantized wrapper module from the QuantSim model
        :param quant_sim_model: Model with simulation ops
        :param module_name: Module name
        :return: Quantized wrapper module or None
        """
        quant_module = None

        for name, module in quant_sim_model.named_modules():
            if name == module_name and isinstance(module, StaticGridQuantWrapper):
                quant_module = module
                break

        return quant_module

    @classmethod
    def _update_modules_with_adarounded_weights(cls, quant_sim: QuantizationSimModel):
        """
        Update every module (Conv and Linear)'s weight parameter with Adarounded weight (Soft rounding)
        :param quant_sim: The QuantSim that contains the model and Adaround tensor quantizers
        """
        # pylint: disable=protected-access
        for quant_module in quant_sim.model.modules():
            if isinstance(quant_module, StaticGridQuantWrapper) and \
                    isinstance(quant_module._module_to_wrap, AdaroundSupportedModules):
                quantizer = quant_module.param_quantizers['weight']

                # It is possible that a module with weights defined in the model may not be used in the
                # forward pass. These modules will not have a AdaroundTensorQuantizer associated with them
                if isinstance(quantizer, AdaroundTensorQuantizer):
                    cls._update_module_params(quant_module._module_to_wrap, quantizer)

    @staticmethod
    def _update_module_params(module: torch.nn.Module, quantizer: AdaroundTensorQuantizer):
        """
        Update module's weight parameter with Adarounded weight
        :param module: module which was Adarounded
        :param quantizer: Tensor quantizer associated with the module
        """
        for param_name, param in module.named_parameters():
            # Only the weight parameter is Adarounded
            if param_name == 'weight':
                orig_weight = param.detach().clone()

                # Use soft rounding to compute Adarounded weight
                quantizer.use_soft_rounding = True
                adaround_weight = quantizer.adaround_weights(orig_weight)

                param.data.zero_()
                param.data.add_(adaround_weight.data)

    @classmethod
    def _export_encodings_to_json(cls, path: str, filename_prefix: str, quant_sim: QuantizationSimModel):
        """
        Save Adadrounded module's parameter encodings to JSON file
        :param path: path where to store param encodings
        :param filename_prefix: filename to store exported weight encodings in JSON format
        :param quant_sim: QunatSim that contains the model and Adaround tensor quantizers
        """
        # pylint: disable=protected-access
        # Create a dictionary to export to JSON file
        param_encodings = {}

        for name, quant_module in quant_sim.model.named_modules():
            if isinstance(quant_module, StaticGridQuantWrapper) and \
                    isinstance(quant_module._module_to_wrap, AdaroundSupportedModules):
                quantizer = quant_module.param_quantizers['weight']

                if isinstance(quantizer, AdaroundTensorQuantizer):
                    cls._update_param_encodings_dict(quant_module, name, param_encodings)

        # export encodings to JSON file
        encoding_file_path = os.path.join(path, filename_prefix + '.encodings')
        with open(encoding_file_path, 'w') as encoding_fp:
            json.dump(param_encodings, encoding_fp, sort_keys=True, indent=4)

    @classmethod
    def _update_param_encodings_dict(cls, quant_module: StaticGridQuantWrapper, name: str, param_encodings: Dict):
        """
        Add module's weight parameter encodings to dictionary to be used for exporting encodings
        :param quant_module: quant module
        :param name: name of module
        :param param_encodings: Dictionary of param encodings
        """
        for orig_param_name, param_quantizer in quant_module.param_quantizers.items():
            if orig_param_name == 'weight':
                param_name = name + '.' + orig_param_name
                encodings = cls._create_encodings_dict_for_quantizer(param_quantizer)
                param_encodings[param_name] = [encodings]

    @staticmethod
    def _create_encodings_dict_for_quantizer(quantizer: AdaroundTensorQuantizer) -> Dict:
        """
        Return encodings for given qunatizer
        :param quantizer: Tensor quantizer associated with module's param
        :return: Dictionary containing encodings
        """
        return {'min': quantizer.encoding.min,
                'max': quantizer.encoding.max,
                'scale': quantizer.encoding.delta,
                'offset': quantizer.encoding.offset,
                'bitwidth': quantizer.encoding.bw,
                'is_symmetric': str(quantizer.use_symmetric_encodings),
                'dtype': 'int' if quantizer.data_type == QuantizationDataType.int else 'float'}

    @staticmethod
    def _override_param_bitwidth(model: torch.nn.Module, quant_sim: QuantizationSimModel,
                                 param_bw_override_list: List[Tuple[torch.nn.Module, int]]):
        """

        For the QuantSim, for the list of modules in the param_bw_override_list,
        overrides the default parameter bitwidths with the provided bitwidth.

        :param model: The original model
        :param quant_sim: The QuantSim that was created using a deepcopy of the original model.
        :param param_bw_override_list: List of Tuples. Each Tuple is a module and the corresponding parameter bitwidth
                                       to be used for that module.
        :return:
        """

        if param_bw_override_list:

            # Create a mapping of original model's AdaRoundable module and their name
            module_to_name = {}
            for name, module in model.named_modules():
                if isinstance(module, AdaroundSupportedModules):
                    module_to_name[module] = name

            # Create a mapping of QuantSim model's AdaRoundable module name and their module
            name_to_module = {}
            for q_name, q_module in quant_sim.model.named_modules():
                if isinstance(q_module, QcQuantizeWrapper):
                    if isinstance(q_module._module_to_wrap, AdaroundSupportedModules):  # pylint: disable=protected-access
                        name_to_module[q_name] = q_module

            # For the modules specified in the param_bw_override_list, set the weight quantizer bitwidth
            for module_bw in param_bw_override_list:
                module, bw = module_bw
                module_name = module_to_name[module]
                quant_wrapper = name_to_module[module_name]
                if isinstance(quant_wrapper, QcQuantizeWrapper):
                    quant_wrapper.param_quantizers['weight'].bitwidth = bw

    @classmethod
    def _skip_quantization_for_ops(cls, model: torch.nn.Module, quant_sim: QuantizationSimModel,
                                   ignore_quant_ops_list: List[torch.nn.Module]):
        """
        For the Ops mentioned in the ignore_quant_ops_list, remove the corresponding Quantization wrappers from the
        QuantSim object.

        :param model: The original model
        :param quant_sim: The QuantSim that was created using a deepcopy of the original model.
        :param ignore_quant_ops_list: The list of Ops for which the Quantization wrappers are removed from the
                                      QuantSim object.
        :return:
        """
        list_of_modules_to_remove = []
        for module in ignore_quant_ops_list:
            layer_name = utils.get_layer_name(model, module)
            list_of_modules_to_remove.append(cls._get_quant_module(quant_sim.model, layer_name))
        quant_sim.exclude_layers_from_quantization(list_of_modules_to_remove)
