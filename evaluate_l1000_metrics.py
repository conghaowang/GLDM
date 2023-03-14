from dataset import LincsDataset
from model import BaseModel
from aae import AAE
from model_utils import get_params
from rdkit.Chem import RDConfig
import itertools
from rdkit import Chem
import os
import sys
import pandas as pd
from rdkit import RDLogger
import pickle
from l1000_evaluation_utils import compute_max_similarity
import argparse

lg = RDLogger.logger()

lg.setLevel(RDLogger.CRITICAL)
from tqdm import tqdm

sys.path.append(os.path.join(RDConfig.RDContribDir, "SA_Score"))
import sascorer
import torch
import numpy as np


def generate_similar_molecules_with_gene_exp_diff(
    control_idx,
    tumour_idx,
    original_idx,
    dataset,
    model,
    rand_vect_dim=512,
    num_samples=20,
    device="cuda:0",
):
    model = model.to(device=device)
    possible_pairs = np.array(list(itertools.product(control_idx, tumour_idx)))

    control_idx_batched = possible_pairs[:, 0]
    tumour_idx_batched = possible_pairs[:, 1]

    control_gene_exp_batched = dataset._gene_exp_controls[control_idx_batched]
    tumour_gene_exp_batched = dataset._gene_exp_tumour[tumour_idx_batched]
    difference_gene_exp_batched = tumour_gene_exp_batched - control_gene_exp_batched

    # Create num_samples//num_diff_vectors random vectors
    if num_samples > difference_gene_exp_batched.shape[0]:
        num_rand_vectors_required = num_samples // difference_gene_exp_batched.shape[0]
        random_vectors = torch.randn(
            num_rand_vectors_required, rand_vect_dim, device=device
        )
        # repeat each gene expression difference vector in its place a number of times
        # equal to the number of random vectors using repeat_interleave
        # then repeat the random vectors batchwise so that we can align the random vectors
        # with the gene expression differences
        # Eg given 114 gene expression diff vectors, we will have 8 random vectors
        # then for each gene expresison vector, we want to match it with each of the
        # 8 random vectors individually
        difference_gene_exp_batched = torch.tensor(
            difference_gene_exp_batched, device=device
        )
        difference_gene_exp_batched = torch.repeat_interleave(
            difference_gene_exp_batched, num_rand_vectors_required, dim=0
        )
        random_vectors = random_vectors.repeat(possible_pairs.shape[0], 1)

    else:
        num_rand_vectors_required = num_samples
        # since number of samples is less than the number of gene expressions
        # we need to truncate the gene expressions too
        difference_gene_exp_batched = torch.tensor(
            difference_gene_exp_batched[:num_samples, :], device=device
        )
        random_vectors = torch.randn(
            num_rand_vectors_required, rand_vect_dim, device=device
        )

    dose_batched = (
        torch.from_numpy(
            np.repeat(
                dataset._experiment_idx_to_dose[original_idx], (random_vectors.shape[0])
            )
        )
        .float()
        .to(device=device)
    )

    conditioned_random_vectors = model.condition_on_gene_expression(
        latent_representation=random_vectors,
        gene_expressions=difference_gene_exp_batched,
        dose=dose_batched,
    )

    # compute similarity score between all 1000 generated molecules and the actual molecule
    # take the max similarity score
    decoder_states = model.decode(
        latent_representations=conditioned_random_vectors, max_num_steps=120
    )
    molecules = [decoder_state.molecule for decoder_state in decoder_states]

    return molecules


def create_tensors_gene_exp_diff(
    control_idx,
    tumour_idx,
    original_idx,
    dataset,
    num_samples=20,
):
    possible_pairs = np.array(list(itertools.product(control_idx, tumour_idx)))

    control_idx_batched = possible_pairs[:, 0]
    tumour_idx_batched = possible_pairs[:, 1]

    control_gene_exp_batched = dataset._gene_exp_controls[control_idx_batched]
    tumour_gene_exp_batched = dataset._gene_exp_tumour[tumour_idx_batched]
    difference_gene_exp_batched = tumour_gene_exp_batched - control_gene_exp_batched

    # Create num_samples//num_diff_vectors random vectors
    if num_samples > difference_gene_exp_batched.shape[0]:
        num_rand_vectors_required = num_samples // difference_gene_exp_batched.shape[0]
        random_vectors = torch.randn(num_rand_vectors_required, 512)
        # repeat each gene expression difference vector in its place a number of times
        # equal to the number of random vectors using repeat_interleave
        # then repeat the random vectors batchwise so that we can align the random vectors
        # with the gene expression differences
        # Eg given 114 gene expression diff vectors, we will have 8 random vectors
        # then for each gene expresison vector, we want to match it with each of the
        # 8 random vectors individually
        difference_gene_exp_batched = torch.tensor(difference_gene_exp_batched)
        difference_gene_exp_batched = torch.repeat_interleave(
            difference_gene_exp_batched, num_rand_vectors_required, dim=0
        )
        random_vectors = random_vectors.repeat(possible_pairs.shape[0], 1)

    else:
        num_rand_vectors_required = num_samples
        # since number of samples is less than the number of gene expressions
        # we need to truncate the gene expressions too
        difference_gene_exp_batched = torch.tensor(
            difference_gene_exp_batched[:num_samples, :]
        )
        random_vectors = torch.randn(num_rand_vectors_required, 512)

    dose_batched = torch.from_numpy(
        np.repeat(
            dataset._experiment_idx_to_dose[original_idx], (random_vectors.shape[0])
        )
    ).float()

    return random_vectors, difference_gene_exp_batched, dose_batched, original_idx


class GeneExpDataset(torch.utils.data.Dataset):
    def __init__(
        self,
        list_of_random_vectors,
        list_of_difference_gene_exp_batched,
        list_of_dose_batched,
        list_of_original_idxes,
    ):
        self.list_of_random_vectors = list_of_random_vectors
        self.list_of_difference_gene_exp_batched = list_of_difference_gene_exp_batched
        self.list_of_dose_batched = list_of_dose_batched
        self.list_of_original_idxes = list_of_original_idxes

    def __len__(self):
        return len(self.original_idxes)

    def __get_item__(self, idx):
        return (
            self.list_of_random_vectors[idx],
            self.list_of_difference_gene_exp_batched[idx],
            self.list_of_dose_batched[idx],
            self.list_of_original_idxes[idx],
        )


def sanitise(row):
    """Specifically for the L1000 csv"""
    control_indices = (
        row["ControlIndices"]
        .replace("[", "")
        .replace("]", "")
        .replace("\n", "")
        .split(" ")
    )
    control_indices = [idx for idx in control_indices if idx != ""]
    row["ControlIndices"] = np.asarray(control_indices, dtype=np.int32)
    tumour_indices = (
        row["TumourIndices"]
        .replace("[", "")
        .replace("]", "")
        .replace("\n", "")
        .split(" ")
    )
    tumour_indices = [idx for idx in tumour_indices if idx != ""]
    row["TumourIndices"] = np.asarray(tumour_indices, dtype=np.int32)
    return row


dataset = LincsDataset(
    root="/data/ongh0068",
    raw_moler_trace_dataset_parent_folder="/data/ongh0068/guacamol/trace_dir",
    output_pyg_trace_dataset_parent_folder="/data/ongh0068/l1000/l1000_biaae/already_batched",
    split="valid_0",
    gene_exp_controls_file_path="/data/ongh0068/l1000/l1000_biaae/lincs/robust_normalized_controls.npz",
    gene_exp_tumour_file_path="/data/ongh0068/l1000/l1000_biaae/lincs/robust_normalized_tumors.npz",
    lincs_csv_file_path="/data/ongh0068/l1000/l1000_biaae/lincs/experiments_filtered.csv",
)

test_set = pd.read_csv("/data/ongh0068/l1000/l1000_biaae/INPUT_DIR/test.csv")
test_set = test_set.apply(lambda x: sanitise(x), axis=1)


reference_smiles = test_set.SMILES.to_list()
control_idxes = test_set.ControlIndices.values
tumour_idxes = test_set.TumourIndices.values
original_idxes = test_set.original_idx.to_list()

parser = argparse.ArgumentParser()
parser.add_argument("--model_type", type=str, choices=["vae", "aae", "wae"])
args = parser.parse_args()
if args.model_type == "vae":
    ######################## VAE ##########################
    print("evaluating vae")
    vae_lower_lr = (
        "/data/ongh0068/l1000/2023-03-11_23_33_36.921147/epoch=07-val_loss=0.60.ckpt"
    )

    params = get_params(dataset)
    pretrained_model = BaseModel.load_from_checkpoint(
        vae_lower_lr, params=params, dataset=dataset, using_lincs=True
    )

    results = {}

    # collect tensors into lists and then instantiate dataset
    for control_idx, tumour_idx, reference_smile, original_idx in tqdm(
        zip(control_idxes, tumour_idxes, reference_smiles, original_idxes)
    ):
        print("evaluating vae ", original_idx)
        try:
            candidate_molecules = generate_similar_molecules_with_gene_exp_diff(
                control_idx,
                tumour_idx,
                original_idx,
                dataset,
                pretrained_model,
                rand_vect_dim=512,
                num_samples=20,
            )
            results["_".join([reference_smile, str(original_idx)])] = {}
            results["_".join([reference_smile, str(original_idx)])][
                "generated_smiles"
            ] = [Chem.MolToSmiles(mol) for mol in candidate_molecules]
            sa_scores = [sascorer.calculateScore(mol) for mol in candidate_molecules]
            results["_".join([reference_smile, str(original_idx)])][
                "sa_scores"
            ] = sa_scores
        except Exception as e:
            print(e)

    with open("tl_vae_generated_molecules_and_sa_scores2.pkl", "wb") as f:
        pickle.dump(results, f)

    generated_mol_sims = {}
    for reference_smile_original_idx in tqdm(results):
        try:
            reference_smile = reference_smile.rsplit("_", 1)[0]
            max_sim = compute_max_similarity(
                candidate_molecules=[
                    Chem.MolFromSmiles(smile)
                    for smile in results[reference_smile_original_idx][
                        "generated_smiles"
                    ]
                ],
                reference_smile=reference_smile,
            )
            generated_mol_sims[reference_smile_original_idx] = max_sim

        except Exception as e:
            print(e)

    with open("tl_vae_test_set_smile_to_max_sim_generated_molecule2.pkl", "wb") as f:
        pickle.dump(generated_mol_sims, f)
    print("done with vae")
    ######################## VAE ##########################
elif args.model_type == "aae":

    ######################## AAE ##########################
    print("evaluating aae")
    aae_lower_lr = (
        "/data/ongh0068/l1000/2023-03-11_20_54_15.863102/epoch=20-train_loss=0.00.ckpt"
    )

    params = get_params(dataset)
    params["gene_exp_condition_mlp"]["input_feature_dim"] = 832 + 978 + 1
    pretrained_model = AAE.load_from_checkpoint(
        aae_lower_lr,
        params=params,
        dataset=dataset,
        using_lincs=True,
        using_wasserstein_loss=False,
        using_gp=False,
    )

    results = {}

    for control_idx, tumour_idx, reference_smile, original_idx in tqdm(
        zip(control_idxes, tumour_idxes, reference_smiles, original_idxes)
    ):
        print("evaluating aae ", original_idx)
        try:
            candidate_molecules = generate_similar_molecules_with_gene_exp_diff(
                control_idx,
                tumour_idx,
                original_idx,
                dataset,
                pretrained_model,
                rand_vect_dim=832,
                num_samples=20,
            )
            results["_".join([reference_smile, str(original_idx)])] = {}
            results["_".join([reference_smile, str(original_idx)])][
                "generated_smiles"
            ] = [Chem.MolToSmiles(mol) for mol in candidate_molecules]
            sa_scores = [sascorer.calculateScore(mol) for mol in candidate_molecules]
            results["_".join([reference_smile, str(original_idx)])][
                "sa_scores"
            ] = sa_scores
        except Exception as e:
            print(e)

    with open("tl_aae_generated_molecules_and_sa_scores2.pkl", "wb") as f:
        pickle.dump(results, f)

    generated_mol_sims = {}
    for reference_smile_original_idx in tqdm(results):
        try:
            reference_smile = reference_smile.rsplit("_", 1)[0]
            max_sim = compute_max_similarity(
                candidate_molecules=[
                    Chem.MolFromSmiles(smile)
                    for smile in results[reference_smile_original_idx][
                        "generated_smiles"
                    ]
                ],
                reference_smile=reference_smile,
            )
            generated_mol_sims[reference_smile_original_idx] = max_sim

        except Exception as e:
            print(e)
    with open("tl_aae_test_set_smile_to_max_sim_generated_molecule2.pkl", "wb") as f:
        pickle.dump(generated_mol_sims, f)

    print("done with aae")
    ######################## AAE ##########################
elif args.model_type == "wae":

    ######################## WAE ##########################
    print("evaluating wae")
    wae_lower_lr = (
        "/data/ongh0068/l1000/2023-03-11_20_54_14.382629/epoch=20-train_loss=-8.16.ckpt"
    )

    params = get_params(dataset)
    params["gene_exp_condition_mlp"]["input_feature_dim"] = 832 + 978 + 1
    pretrained_model = AAE.load_from_checkpoint(
        wae_lower_lr,
        params=params,
        dataset=dataset,
        using_lincs=True,
        using_wasserstein_loss=True,
        using_gp=True,
    )

    results = {}

    for control_idx, tumour_idx, reference_smile, original_idx in tqdm(
        zip(control_idxes, tumour_idxes, reference_smiles, original_idxes)
    ):
        print("evaluating wae ", original_idx)
        try:
            candidate_molecules = generate_similar_molecules_with_gene_exp_diff(
                control_idx,
                tumour_idx,
                original_idx,
                dataset,
                pretrained_model,
                rand_vect_dim=832,
                num_samples=20,
            )
            results["_".join([reference_smile, str(original_idx)])] = {}
            results["_".join([reference_smile, str(original_idx)])][
                "generated_smiles"
            ] = [Chem.MolToSmiles(mol) for mol in candidate_molecules]
            sa_scores = [sascorer.calculateScore(mol) for mol in candidate_molecules]
            results["_".join([reference_smile, str(original_idx)])][
                "sa_scores"
            ] = sa_scores
        except Exception as e:
            print(e)

    with open("tl_wae_generated_molecules_and_sa_scores2.pkl", "wb") as f:
        pickle.dump(results, f)

    generated_mol_sims = {}
    for reference_smile_original_idx in tqdm(results):
        try:
            reference_smile = reference_smile.rsplit("_", 1)[0]
            max_sim = compute_max_similarity(
                candidate_molecules=[
                    Chem.MolFromSmiles(smile)
                    for smile in results[reference_smile_original_idx][
                        "generated_smiles"
                    ]
                ],
                reference_smile=reference_smile,
            )
            generated_mol_sims[reference_smile_original_idx] = max_sim
        except Exception as e:
            print(e)
    with open("tl_wae_test_set_smile_to_max_sim_generated_molecule2.pkl", "wb") as f:
        pickle.dump(generated_mol_sims, f)

    print("done with wae")

    ######################## WAE ##########################
