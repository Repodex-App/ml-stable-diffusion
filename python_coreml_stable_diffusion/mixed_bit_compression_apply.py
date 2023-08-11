from pprint import pprint
import argparse
import coremltools as ct
import gc
import json
import logging
import numpy as np
import os

from python_coreml_stable_diffusion.torch2coreml import get_pipeline
from python_coreml_stable_diffusion.mixed_bit_compression_pre_analysis import (
    NBITS,
    PALETTIZE_MIN_SIZE as MIN_SIZE
)

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def main(args):
    # Load Core ML model
    coreml_model = ct.models.MLModel(args.mlpackage_path, compute_units=ct.ComputeUnit.CPU_ONLY)
    logger.info(f"Loaded {args.mlpackage_path}")

    # Keep track of precision stats
    precision_stats = {nbits:{'num_tensors': 0, 'numel': 0} for nbits in NBITS}
    
    # Load palettization recipe
    with open(args.pre_analysis_json_path, 'r') as f:
        pre_analysis = json.load(f)

    if args.selected_recipe not in list(pre_analysis["recipes"]):
        raise KeyError(
            f"--selected-recipe ({args.selected_recipe}) not found in "
            f"--pre-analysis-json-path ({args.pre_analysis_json_path}). "
            f" Available recipes: {list(pre_analysis['recipes'])}"
        )


    recipe = pre_analysis["recipes"][args.selected_recipe]
    assert all(nbits in NBITS + [16] for nbits in recipe.values()), \
        f"Some nbits values in the recipe are illegal. Allowed values: {NBITS}"

    # Hash tensors to be able to match torch tensor names to mil tensors
    def get_tensor_hash(tensor):
        """
        This function calculates a unique hash for a given tensor.

        Parameters:
        tensor (np.ndarray): The input tensor for which to calculate the hash.

        Returns:
        float: The calculated hash for the input tensor.
        """
        # Calculate the product of the tensor's shape using tensor.size for better performance
        tensor_size = tensor.size

        # Use the first element of the tensor and the tensor's size to calculate a unique hash
        return tensor.ravel()[0] if tensor.size > 0 else 0 + tensor_size

    args.model_version = pre_analysis["model_version"]
    pipe = get_pipeline(args)
    torch_model = pipe.unet

    hashed_recipe = {}
    for torch_module_name, nbits in recipe.items():
        tensor = [
            tensor.cpu().numpy().astype(np.float16) for name,tensor in torch_model.named_parameters()
            if name == torch_module_name + '.weight'
        ][0]
        hashed_recipe[get_tensor_hash(tensor)] = nbits

    del pipe
    gc.collect()

    current_nbits: int

    def op_selector(const):
        parameter_tensor = const.val.val
        if parameter_tensor.size < MIN_SIZE:
            return False

        if parameter_tensor.dtype != np.float16:
            # These are the tensors that were compressed to look-up indices in previous passes
            return False

        tensor_hash = get_tensor_hash(parameter_tensor)
        tensor_spec = f"{tensor_hash} with shape {parameter_tensor.shape}"


        hashes = list(hashed_recipe)
        pdist = np.abs(np.array(hashes) - tensor_hash)
        matched = pdist.argmin()
        logger.debug(f"{tensor_spec}: {tensor_hash} matched with {hashes[matched]} (hash error={pdist.min()})")

        target_nbits = hashed_recipe[hashes[matched]]
        
        do_palettize = current_nbits == target_nbits
        if do_palettize:
            logger.debug(f"{tensor_spec}: Palettizing to {target_nbits}-bit palette")
            precision_stats[current_nbits]['num_tensors'] += 1
            precision_stats[current_nbits]['numel'] +=  np.prod(parameter_tensor.shape)
            return True
        return False

    for nbits in NBITS:
        logger.info(f"Processing tensors targeting {nbits}-bit palettes")
        current_nbits = nbits

        config = ct.optimize.coreml.OptimizationConfig(
           global_config=ct.optimize.coreml.OpPalettizerConfig(mode="kmeans", nbits=nbits, weight_threshold=None,),
           is_deprecated=True,
           op_selector=op_selector,
        )
        coreml_model = ct.optimize.coreml.palettize_weights(coreml_model, config=config)
        logger.info(f"{precision_stats[nbits]['num_tensors']} tensors are palettized with {nbits} bits")


    tot_numel = sum([precision_stats[nbits]['numel'] for nbits in NBITS])
    final_size = sum([precision_stats[nbits]['numel'] * nbits for nbits in NBITS])
    logger.info(f"Palettization result: {final_size / tot_numel:.2f}-bits resulting in {final_size / (8*1e6)} MB")
    pprint(precision_stats)
    coreml_model.save(args.o)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-o",
        required=True,
        help="Output directory to save the custom palettized model"
    )
    parser.add_argument(
        "--mlpackage-path",
        required=True,
        help="Path to .mlpackage model to be palettized"
    )
    parser.add_argument(
        "--pre-analysis-json-path",
        required=True,
        type=str,
        help=("The JSON file generated by mixed_bit_compression_pre_analysis.py"
    ))
    parser.add_argument(
        "--selected-recipe",
        required=True,
        type=str,
        help=("The string key into --pre-analysis-json-path's baselines dict"
    ))

    args = parser.parse_args()

    if not os.path.exists(args.mlpackage_path):
        raise FileNotFoundError
    if not os.path.exists(args.pre_analysis_json_path):
        raise FileNotFoundError
    if not args.pre_analysis_json_path.endswith('.json'):
        raise ValueError("--recipe-json-path should end with '.json'")

    main(args)