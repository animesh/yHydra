import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pandas.io.pytables import DataCol
from score import calc_ions, scoring
import multiprocessing, sys, os
from tqdm import tqdm

sys.path.append("../Neonomicon")
sys.path.append("../dnovo3")
from preprocessing import normalize_intensities
from utils import batched_list,unbatched_list
from proteomics_utils import theoretical_peptide_mass,trim_peaks_list

import argparse

parser = argparse.ArgumentParser(description='convert')

parser.add_argument('--OUTPUT_DIR', default='./output', type=str, help='directory containing search results')

args = parser.parse_args()

OUTPUT_DIR = args.OUTPUT_DIR

#search_results = pd.read_csv('./search_results.csv')
search_results = pd.read_hdf(os.path.join(OUTPUT_DIR,'search_results.h5'),'search_results')

MAX_N_PEAKS = 500

def trim_peaks_list_(x): 
    mzs, intensities = x
    mzs, intensities = mzs, normalize_intensities(intensities)
    return trim_peaks_list(mzs, intensities,MAX_N_PEAKS=MAX_N_PEAKS)

if __name__ == '__main__':
    k = 50
    batch_size=64
    SUBSET=None

    top_peptides = []
    best_scores = []
    all_scores = []

    with multiprocessing.Pool(64) as p:

        #for i,row in enumerate(search_results.iterrows()):
        for i in tqdm(range(0,len(search_results[:SUBSET]),batch_size)):     
            rows = search_results.iloc[i:i+batch_size] # TODO: fix last batch
            true_peptide = rows['peptide'].to_numpy()
            batched_topk_peptides = rows['topk_peptides'].to_numpy()
            charges = rows['charge'].to_numpy()
            
            apparent_batch_size = len(rows)

            topk_peptides = np.array(unbatched_list(batched_topk_peptides))           
            charges_tiled = np.repeat(charges,k)
            topk_peptides_charge = list(zip(topk_peptides,charges_tiled))
            ions = np.array(list(p.imap(calc_ions,topk_peptides_charge,1)))
            ions = np.reshape(ions,(apparent_batch_size,k,-1))
            #print(len(ions))
            #ions = list(batched_list(ions,batch_size))
            #mzs = np.array(rows['mzs'])
            #intensities = np.array(rows['intensities'])
            mzs = rows['mzs'].tolist()
            intensities = rows['intensities'].tolist()

            mzs, intensities = zip(*list(p.imap(trim_peaks_list_,list(zip(mzs,intensities)))))
            #mzs, intensities = list(map(trim_ions,rows['mzs'])),list(map(trim_ions,rows['intensities']))
            #mzs, intensities = np.array(rows['mzs']),np.array(rows['intensities'])
            #mzs, intensities = np.expand_dims(mzs,0), np.expand_dims(intensities,0)

            mzs = np.array(mzs)
            intensities = np.array(intensities)
            #ions = np.array(ions)
            #print(ions.shape)
            #ions = np.transpose(ions,(1,0,2))

            # print(mzs.shape)
            # print(intensities.shape)

            best_score_index, best_score, pos_score = scoring(mzs, intensities, ions)        

            top_peptide = [batched_topk_peptides[id][b] for id,b in enumerate(best_score_index)]
            print(sum(top_peptide==true_peptide))
            #print(sum(top_peptide==true_peptide),top_peptide,true_peptide,best_score)
            # if i > 10:
            #     quit()

            top_peptides.extend(top_peptide)
            best_scores.extend(best_score)
            all_scores.extend(np.reshape(pos_score,-1))

    search_results = search_results[:SUBSET]

    search_results['best_score']=best_scores
    search_results['best_peptide']=top_peptides
    search_results['peptide_mass']= list(map(lambda x: theoretical_peptide_mass(*x),zip(top_peptides,np.zeros_like(top_peptides))))
    search_results['delta_mass']=search_results['pepmass'] - search_results['peptide_mass']

    print(sum(search_results['best_peptide']==search_results['peptide'])/len(search_results))

    print(search_results)
    print(search_results.columns)

    #search_results.to_csv('search_results_scored.csv',index=False)
    search_results.to_hdf(os.path.join(OUTPUT_DIR,'search_results_scored.h5'),key='search_results_scored', mode='w')

    plt.hist(np.log(np.squeeze(all_scores)+1.),bins=100)
    plt.hist(np.log(np.squeeze(best_scores)+1.),bins=100)
    plt.yscale('log')
    plt.savefig('./figures/hit_score_dist.png')

    quit()


    topk_hits = []
    hit_distances = []

    for row in search_results.iterrows():
        true_peptide = row[1]['peptide']
        topk_peptides = row[1]['topk_peptides']
        topk_distances = row[1]['topk_distances']

        topk_hits.append(true_peptide in topk_peptides)
        if true_peptide in set(topk_peptides):
            hit_index = topk_peptides == true_peptide
            hit_distance = topk_distances[hit_index]
            hit_distances.extend(hit_distance)
        else:
            pass
            #hit_distances.extend([0])

    search_results['topk_hits'] = topk_hits

    print(sum(topk_hits)/len(topk_hits))

    flatten = lambda x: [item for sublist in x for item in sublist]

    set_true_peptides = set(search_results.peptide)
    set_predicted_peptides = set(flatten([topk_peptides for topk_peptides in search_results.topk_peptides]))
    set_hits = set_true_peptides.intersection(set_predicted_peptides)

    print(len(set_true_peptides),len(set_predicted_peptides),len(set_hits))

    np.savetxt('predicted_peptides.txt',np.array(list(set_predicted_peptides)),fmt="%s")

    all_distances = flatten([topk_distances for topk_distances in search_results.topk_distances])

    plt.hist(all_distances)
    plt.hist(hit_distances)
    plt.yscale('log')
    plt.savefig('./figures/hit_score_dist.png')