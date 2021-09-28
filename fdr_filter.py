import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib_venn import venn2
#from score import calc_ions, scoring
from tqdm import tqdm
from pyteomics import auxiliary as aux
import os

import argparse

parser = argparse.ArgumentParser(description='convert')

parser.add_argument('--OUTPUT_DIR', default='./output', type=str, help='directory containing search results')
parser.add_argument('--REV_OUTPUT_DIR', default='./rev', type=str, help='directory containing search results (reverse decoy)')

args = parser.parse_args()

OUTPUT_DIR = args.OUTPUT_DIR
REV_OUTPUT_DIR = args.REV_OUTPUT_DIR

search_results = pd.read_hdf(os.path.join(OUTPUT_DIR,'search_results_scored.h5'),'search_results_scored')
rev_search_results = pd.read_hdf(os.path.join(REV_OUTPUT_DIR,'search_results_scored.h5'),'search_results_scored')

search_results['is_decoy'] = False
rev_search_results['is_decoy'] = True

df = pd.concat([search_results,rev_search_results])
df.best_score = -np.log(df.best_score+1.)
df_filtered = aux.filter(df, key='best_score', is_decoy='is_decoy', fdr=0.01)
df_filtered = df_filtered[~df_filtered.is_decoy]

print(df_filtered)
print(sum(df_filtered['best_peptide']==df_filtered['peptide'])/len(df_filtered))
ground_truth_ident_peptides=set(df.peptide.unique())
yhydra_ident_peptides=set(df_filtered.best_peptide.unique())
print('Identified peptides (true):',len(ground_truth_ident_peptides))
print('Identified peptides (yHydra):',len(yhydra_ident_peptides))

plt.figure(figsize=(7,3))
plt.subplot(1,2,1)
plt.hist(np.log(np.squeeze(search_results['best_score'])+1.),bins=100,label='targets',alpha=0.3)
plt.hist(np.log(np.squeeze(rev_search_results['best_score'])+1.),bins=100,label='decoys',alpha=0.3)
plt.xlabel('yHydra log score')
#plt.yscale('log')
plt.legend()
 
plt.subplot(1,2,2)
venn2([ground_truth_ident_peptides,yhydra_ident_peptides],set_labels=['Ground Truth','yHydra'])
plt.tight_layout()
plt.savefig('./figures/hit_score_dist.png',dpi=600)

new_identified = yhydra_ident_peptides - ground_truth_ident_peptides
print(df_filtered[df_filtered.best_peptide.isin(new_identified)])
search_results.to_hdf(os.path.join(OUTPUT_DIR,'search_results_scored_filtered.h5'),key='search_results_scored_filtered', mode='w')
