import rdkit
from rdkit import Chem
from rdkit.Chem import AllChem
import pandas as pd
import numpy as np
import pickle
from tqdm import tqdm
import os
from collections import defaultdict
import re
import subprocess
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('-m', '--model', type=int, default=1, help='model number')

args = parser.parse_args()
model_num = args.model
model = ['vae', 'aae', 'wae', 'biaae'][model_num]

meta_data = pd.read_csv('protein/metadata.csv', header=0)
meta_data = meta_data.iloc[15:18, :]
print(meta_data)
for index, row in meta_data.iterrows():
    protein = row['protein']
    smiles = row['smiles']
    pdb_id = row['pdb']
    lig_id = row['ligand']
    log_path = os.path.join('logs/' + model, pdb_id)
    out_path = os.path.join('poses/' + model, pdb_id)
    os.makedirs(log_path, exist_ok=True)
    os.makedirs(out_path, exist_ok=True)
    protein_file = 'protein/' + pdb_id + '_protein.pdb'
    autobox_ligand_file = 'pocket/' + pdb_id + '_pocket.pdb'
    if os.path.exists(protein_file) == False or os.path.exists(autobox_ligand_file) == False:
        print('protein or autobox ligand file does not exist')
        raise ValueError
    ligand_path = os.path.join('ligand', model, smiles)

    for filename in os.listdir(ligand_path):
        ligand_file = os.path.join(ligand_path, filename)
        if os.path.exists(ligand_file) == False:
            print('ligand file does not exist')
            raise ValueError
        generated_smi = filename.split('.')[0]
        log_file = os.path.join(log_path, generated_smi + '.log')
        out_file = os.path.join(out_path, generated_smi + '.sdf')
        subprocess.run(['gnina', '-r', protein_file, '-l', ligand_file, '--autobox_ligand', autobox_ligand_file, '-o', out_file, '--exhaustiveness', '16', '--log', log_file, '-q'], stdout=subprocess.DEVNULL)
