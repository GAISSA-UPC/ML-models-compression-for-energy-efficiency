"""
Optimization utils
==================

.. module:: optimize_utils
   :platform: Linux
   :synopsis: Optimization auxiliary functions.

.. module_author:: Alec Lagarde Teixidó <aleclagarde@gmail.com>

This script contains auxiliary functions for the optimization part of the project, such as the functions that perform
the optimizations, a function needed to convert back from TFLite model to TensorFlow model, and one to add the
measurements to the results table.

.. autosummary::
   :toctree: generated/

   prune_torch
   prune_tf
   quantize_torch
   quantize_tf
   convert_tflite_to_tf
   add_measurements
"""

# PyTorch libraries
import torch
from torch.nn.utils import prune

# TensorFlow libraries
import tensorflow as tf
import tensorflow_model_optimization as tfmot

# DataFrame modification
import pandas as pd

# Emissions measuring
from codecarbon import track_emissions


@track_emissions
def prune_torch(model, model_name: str, cf: float, cv: bool):
    """
    This function prunes a pretrained PyTorch model from the transformers library given the pruning coefficient and
    saves the pruned model in disk.

    :param model: PyTorch model from the transformers' library.
    :param model_name: Short model name (eg. 't5').
    :param cf: Pruning coefficient.
    :param cv: Whether the model is from the Computer Vision domain.
    """
    if cv:
        if model_name == 'resnet':
            to_prune = model.classifier[-1]
        else:
            to_prune = model.classifier
        prune.l1_unstructured(to_prune, name='weight', amount=cf)
    else:
        for name, module in model.named_modules():
            if isinstance(module, torch.nn.Linear) and "weight" in name:
                prune.l1_unstructured(module, name='weight', amount=cf)

    # Save the pruned model to disk
    model.save_pretrained(f"saved/{model_name}-torch-pruned")


@track_emissions
def prune_tf(model, model_name: str, cf: float):
    """
    This function prunes a pretrained TensorFlow model from the transformers library given the pruning coefficient and
    saves the pruned model in disk.

    :param model: TensorFlow model from the transformers' library.
    :param model_name: Short model name (eg. 't5').
    :param cf: Pruning coefficient.
    """
    # Define the pruning parameters
    pruning_params = {
        'pruning_schedule': tfmot.sparsity.keras.ConstantSparsity(target_sparsity=cf, begin_step=0, end_step=-1,
                                                                  frequency=100),
        'block_size': (1, 1)
    }

    # Loop through the model's submodules and prune its parameters
    for submodule in model.submodules:
        if 'mlp' in submodule.name or 'dense' in submodule.name or 'convolution' in submodule.name:
            # Create a custom PrunableLayer for the unsupported layer
            class PrunableLayer(tf.keras.layers.Layer, tfmot.sparsity.keras.PrunableLayer):
                def __init__(self, layer):
                    super(PrunableLayer, self).__init__()
                    self.layer = layer

                def get_prunable_weights(self):
                    return self.layer.trainable_weights

            # Prune the custom PrunableLayer
            prunable_layer = PrunableLayer(submodule)
            tfmot.sparsity.keras.prune_low_magnitude(prunable_layer, **pruning_params)

    # `prune_low_magnitude` requires a recompile.
    model.compile(optimizer='adam', loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                  metrics=['accuracy'])

    # Save the pruned model to disk
    model.save_pretrained(f"saved/{model_name}-tf-pruned")


@track_emissions
def quantize_torch(model, model_name: str):
    """
    This function quantize a pretrained PyTorch model from the transformers library and saves the quantized model in
    disk.

    :param model: PyTorch model from the transformers' library.
    :param model_name: Short model name (eg. 't5').
    """
    # Quantize the model
    quantized_model = torch.quantization.quantize_dynamic(
        model,
        {torch.nn.Linear},
        dtype=torch.qint8
    )

    # Save the quantized model
    torch.save(quantized_model, f"saved/{model_name}-torch-quantized.pth")


@track_emissions
def quantize_tf(model):
    """
    This function quantize a pretrained TensorFlow model from the transformers library and returns the pruned model.
    This pruned model is a TFLite model, so there is the following function to convert it back to a TensorFlow model.

    :param model: TensorFlow model from the transformers' library.
    :return: TFLite quantized model.
    """
    # Convert the model to TensorFlow Lite format
    converter = tf.lite.TFLiteConverter.from_keras_model(model=model)
    converter.target_spec.supported_ops = [
        tf.lite.OpsSet.TFLITE_BUILTINS,
        tf.lite.OpsSet.SELECT_TF_OPS
    ]

    # Convert to TF Lite with quantization
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    tflite_model = converter.convert()

    return tflite_model


def tflite_to_keras(model, model_name: str):
    """
    This function converts the quantized TFLite model into a TensorFlow model to perform inference and saves it in disk.

    :param model: TFLite quantized model.
    :param model_name: Short model name (eg. 't5').
    """
    # Load the TensorFlow Lite model into an interpreter
    interpreter = tf.lite.Interpreter(model_content=model)
    interpreter.allocate_tensors()

    # Extract the input and output details
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    # Create a new Sequential model
    tf_model = tf.keras.models.Sequential()

    # Iterate through the input details and add input layers to the model
    for input_detail in input_details:
        shape = input_detail['shape']
        dtype = input_detail['dtype']
        input_layer = tf.keras.layers.Input(shape=shape[1:], dtype=dtype)
        tf_model.add(layer=input_layer)

    # Iterate through the output details and add output layers to the model
    for output_detail in output_details:
        shape = output_detail['shape']
        dtype = output_detail['dtype']
        output_layer = tf.keras.layers.Input(shape=shape[1:], dtype=dtype)
        tf_model.add(layer=output_layer)

    tf_model.compile(optimizer='adam', loss='categorical_crossentropy', metrics=['accuracy'])

    # Save the TensorFlow model to disk
    tf.keras.models.save_model(tf_model, f"saved/{model_name}-tf-quantized")


def add_measurements(dataframe: pd.DataFrame, number_of_measurements: int, model_name: str, framework: str,
                     strategy: str) -> pd.DataFrame:
    """
    This function takes a Pandas DataFrame and adds *number_of_measurements* rows with the string *information* in the
    'information' column, along with the measurement number in the 'iteration' column. This is used to add emissions
    measurements to the results table.

    :param dataframe: Pandas DataFrame to add rows.
    :param number_of_measurements: Number of measurements (rows) to add.
    :param model_name: String to store in the new rows' model column. It is also used to get the domain.
    :param framework: String to store in the new rows' framework column.
    :param strategy: String to store in the new rows' strategy column.
    :return: Pandas DataFrame with the added measurements.
    """
    new_measurements = pd.read_csv(filepath_or_buffer='emissions.csv').tail(n=number_of_measurements)

    if model_name in ['bert', 'gpt2', 't5']:
        domain = 'NLP'
    elif model_name in ['resnet', 'vit', 'convnext']:
        domain = 'CV'
    else:
        domain = 'CG'

    new_measurements['domain'] = domain
    new_measurements['model'] = model_name
    new_measurements['framework'] = framework
    new_measurements['strategy'] = strategy
    new_measurements['iteration'] = [x for x in range(1, number_of_measurements+1)]
    return pd.concat([dataframe, new_measurements], axis=0)
