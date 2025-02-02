import sys

import argparse

parser = argparse.ArgumentParser(description='convert')
parser.add_argument('--DB_DIR', default='./DB', type=str, help='path to db file')
parser.add_argument('--DECOY_DB_DIR', default='./DB', type=str, help='path to db file')
parser.add_argument('--MGF', default=None, type=str, help='path to mgf files')
parser.add_argument('--JSON_DIR', default='./tmp', type=str, help='path to json files')
parser.add_argument('--OUTPUT_DIR', default='./output', type=str, help='directory containing search results')
parser.add_argument('--DEBUG_N', default=None, type=int, help='sample N spectra (fpr debugging)')
parser.add_argument('--GPU', default='-1', type=str, help='GPU id')

args = parser.parse_args()

from tensorflow.python.eager.context import device 
sys.path.append("../dnovo3")
#sys.path.append("../Neonomicon")
import glob, os, json

if args.GPU == '-1':
    os.environ["CUDA_VISIBLE_DEVICES"] = args.GPU
    import tensorflow as tf
    device = '/CPU:0'
    use_gpu=False    
else:
    os.environ["CUDA_VISIBLE_DEVICES"] = args.GPU
    import tensorflow as tf
    device = '/GPU:0'
    physical_devices = tf.config.list_physical_devices('GPU')
    tf.config.experimental.set_memory_growth(physical_devices[0],True)
    use_gpu=True


import random
#from tf_data_json import USIs,parse_json_npy
#from usi_magic import parse_usi
from proteomics_utils import parse_mgf_npy
from proteomics_utils import normalize_intensities,trim_peaks_list_v2,MAX_N_PEAKS,NORMALIZATION_METHOD
from load_model import spectrum_embedder,sequence_embedder
from proteomics_utils import theoretical_peptide_mass,precursor2peptide_mass

from tqdm import tqdm
import numpy as np
import multiprocessing
from load_config import CONFIG

K = CONFIG['K']
N_BUCKETS_NARROW = CONFIG['N_BUCKETS_NARROW']
N_BUCKETS_OPEN = CONFIG['N_BUCKETS_OPEN']
BATCH_SIZE = CONFIG['BATCH_SIZE']
USE_STREAM = CONFIG['USE_STREAM']

AUTOTUNE=tf.data.AUTOTUNE

MSMS_OUTPUT_IN_RESULTS=True

DB_DIR = args.DB_DIR
DECOY_DB_DIR = args.DECOY_DB_DIR
#DB_DIR = './db'
#DB_DIR = './db_miscleav_1'
#DB_DIR = '../../Neonomicon/PXD007963/db'

JSON_DIR = args.JSON_DIR
OUTPUT_DIR = args.OUTPUT_DIR

db_embedded_peptides = np.load(os.path.join(DB_DIR,"embedded_peptides.npy"))
db_peptides = np.load(os.path.join(DB_DIR,"peptides.npy"))

decoy_db_embedded_peptides = np.load(os.path.join(DECOY_DB_DIR,"embedded_peptides.npy"))
decoy_db_peptides = np.load(os.path.join(DECOY_DB_DIR,"peptides.npy"))


print('fire up datasets...')
#N = None
N = args.DEBUG_N
#N = 163410

import sys,io
#file = args.MGF#"/hpi/fs00/home/tom.altenburg/scratch/yHydra_testing/PXD007963/raw/qe2_03132014_11trcRBC-2.mgf"
file = args.MGF
if USE_STREAM:
    stream=sys.stdin.buffer.read()
    stream=io.BytesIO(stream)
else:
    stream=file

true_peptides = []
true_precursorMZs = []
true_pepmasses = []
true_charges = []
true_ID = []
true_mzs = []
true_intensities = []
preprocessed_spectra = []

input_specs_npy = {
    "mzs": np.float32,
    "intensities": np.float32,
    "usi": str,
    #"charge": float,
    "precursorMZ": float,
}

def parse_json_npy_(file_location): return parse_json_npy(file_location,specs=input_specs_npy)

if __name__ == '__main__':


    with multiprocessing.Pool(64) as p:
        print('getting scan information...')
        for i,spectrum in enumerate(tqdm(parse_mgf_npy(stream))):
            mzs = spectrum['mzs']
            intensities = spectrum['intensities']
            if MSMS_OUTPUT_IN_RESULTS:
                true_mzs.append(mzs)
                true_intensities.append(intensities)
            mzs = np.array(mzs)
            intensities = np.array(intensities)
            #mzs, intensities = mzs,normalize_intensities(intensities,method=NORMALIZATION_METHOD)
            #mzs, intensities = trim_peaks_list_v2(mzs, intensities, MAX_N_PEAKS=MAX_N_PEAKS, PAD_N_PEAKS=500)
            preprocessed_spectrum = np.stack((mzs, intensities),axis=-1)
            preprocessed_spectra.append(preprocessed_spectrum)

            charge=int(spectrum['charge'])
            precursorMZ=float(spectrum['precursorMZ'])
            scans=int(spectrum['scans'])

            true_precursorMZs.append(precursorMZ)
            true_pepmasses.append(precursor2peptide_mass(precursorMZ,int(charge)))
            true_charges.append(int(charge))
            true_ID.append(scans)
            true_peptides.append('')
            # if i>1000-2:
            #     break
    print(len(true_peptides),len(true_precursorMZs),len(true_pepmasses),len(true_charges),len(true_ID),len(true_mzs),len(true_intensities))

    #print(list(zip(true_pepmasses,theoretical_pepmasses)))
    with tf.device(device):
        print('embedding spectra...')
        for _ in tqdm(range(1)): 
            ds_spectra = np.array(preprocessed_spectra)
            embedded_spectra = spectrum_embedder.predict(ds_spectra,batch_size=BATCH_SIZE)
            #print('embedding peptides...')
            #embedded_peptides = sequence_embedder.predict(ds_peptides)
    print(embedded_spectra.shape)

    def append_dim(X,new_dim,axis=1):
        return np.concatenate((X, np.expand_dims(new_dim,axis=axis)), axis=axis)

    embedded_spectra = embedded_spectra

    from sklearn.neighbors import NearestNeighbors
    import faiss

    #query = embedded_peptides
    query = embedded_spectra
    db = np.concatenate([db_embedded_peptides,decoy_db_embedded_peptides])
    db_target_decoy_peptides = np.concatenate([db_peptides,decoy_db_peptides])
    db_is_decoy = np.concatenate([np.zeros(len(db_embedded_peptides),bool),np.ones(len(decoy_db_embedded_peptides),dtype=bool)])

    ####### MASS BUCKETS #######
    ######################################

    from mass_buckets import bucket_indices, get_peptide_mass, MIN_PEPTIDE_MASS, MAX_PEPTIDE_MASS, add_bucket_adress

    print('calc masses ...')
    db_pepmasses = np.array(list(map(get_peptide_mass,tqdm(db_target_decoy_peptides))))

    inmassrange_indices =  (db_pepmasses >= MIN_PEPTIDE_MASS) & (db_pepmasses <= MAX_PEPTIDE_MASS)
    db_pepmasses = db_pepmasses[inmassrange_indices]
    db_target_decoy_peptides = db_target_decoy_peptides[inmassrange_indices]
    db = db[inmassrange_indices,:]
    db_is_decoy = db_is_decoy[inmassrange_indices]

    ######################################
    ######### NARROW
    buckets,est = bucket_indices(db_pepmasses,'uniform',N_BUCKETS_NARROW)

    db_narrow = add_bucket_adress(db,db_pepmasses,est,N_BUCKETS=N_BUCKETS_NARROW)
    
    embedded_spectra_narrow = add_bucket_adress(embedded_spectra,true_pepmasses,est,0,N_BUCKETS=N_BUCKETS_NARROW)

    ######### NARROW
    ######################################

    ######################################
    ######### OPEN
    buckets,est = bucket_indices(db_pepmasses,'uniform',N_BUCKETS_OPEN)

    db_open = add_bucket_adress(db,db_pepmasses,est,N_BUCKETS=N_BUCKETS_OPEN)
    
    embedded_spectra_open_0 = add_bucket_adress(embedded_spectra,true_pepmasses,est,0,N_BUCKETS=N_BUCKETS_OPEN)

    embedded_spectra_open_m1 = add_bucket_adress(embedded_spectra,true_pepmasses,est,-1,N_BUCKETS=N_BUCKETS_OPEN)
    embedded_spectra_open_p1 = add_bucket_adress(embedded_spectra,true_pepmasses,est,+1,N_BUCKETS=N_BUCKETS_OPEN)
    ######### OPEN
    ######################################

    ######################################

    #query = embedded_spectra
    ####### MASS BUCKETS #######
    ######################################

    #query = append_dim(embedded_peptides,true_pepmasses)
    #query = append_dim(embedded_spectra,theoretical_pepmasses)
    #db = append_dim(db_embedded_peptides,db_pepmasses)

    #query = np.expand_dims(true_pepmasses,axis=-1)
    #db = np.expand_dims(db_pepmasses,axis=-1)

    norm = lambda x : np.sqrt(np.inner(x,x))
    diff = lambda x,y : norm(x-y)

    def get_index(DB,k=50,metric='euclidean',method='sklearn',use_gpu=use_gpu):
        print('indexing...')
        if method=='sklearn':
            if metric=='euclidean':
                p=2        
            for _ in tqdm(range(1)):
                index = NearestNeighbors(n_neighbors=k,p=p,n_jobs=1)
                index.fit(DB)
            return index

        if method=='faiss':
            for _ in tqdm(range(1)):
                d = DB.shape[-1]
                if metric=='euclidean':
                    index_flat = faiss.IndexFlatL2(d)
                    #index_flat = faiss.IndexIVFFlat(index_flat, d, 100)
                if use_gpu:
                    res = faiss.StandardGpuResources()
                    index_flat = faiss.index_cpu_to_gpu(res, 0, index_flat)
                index_flat.add(DB)
            return index_flat

    def perform_search(query,k,index,method='sklearn'):
        print('searching...')
        if method=='sklearn':           
            for _ in tqdm(range(1)):
                D,I = index.kneighbors(query, k, return_distance=True)
                return D,I
        if method=='faiss':           
            for _ in tqdm(range(1)):
                D,I = index.search(query, k)
                return D,I

    print(db.shape)

    index = get_index(db_narrow,k=K,metric='euclidean',method='faiss',use_gpu=use_gpu)
    D_narrow,I_narrow = perform_search(query=embedded_spectra_narrow,k=K,index=index,method='faiss')

    index = get_index(db_open,k=K,metric='euclidean',method='faiss',use_gpu=use_gpu)
    D_open,I_open = perform_search(query=embedded_spectra_open_0,k=K,index=index,method='faiss')

    D=np.concatenate([D_narrow,D_open],axis=-1)
    I=np.concatenate([I_narrow,I_open],axis=-1)

    #index = get_index(db,k=K,metric='euclidean',method='faiss',use_gpu=use_gpu)
    #D,I = perform_search(query=embedded_spectra,k=K,index=index,method='faiss')

    # D_m1,I_m1 = perform_search(query=embedded_spectra_m1,k=K,index=index,method='faiss')
    # D_0,I_0 = perform_search(query=embedded_spectra_0,k=K,index=index,method='faiss')
    # D_p1,I_p1 = perform_search(query=embedded_spectra_p1,k=K,index=index,method='faiss')

    # D=np.concatenate([D_m1,D_0,D_p1],axis=-1)
    # I=np.concatenate([I_m1,I_0,I_p1],axis=-1)

    print(D.shape,I.shape)

    ####### SEARCH RESULTS DATAFRAME #######
    ######################################
    import pandas as pd

    is_decoy           =  list(db_is_decoy[I])
    predicted_peptides = list(db_target_decoy_peptides[I])
    predicted_distances = list(D)

    if not MSMS_OUTPUT_IN_RESULTS:
        true_mzs,true_intensities = None,None

    print(len(predicted_peptides),len(predicted_distances))
    raw_file=os.path.splitext(os.path.basename(file))[0]
    search_results = pd.DataFrame({
                                'raw_file':raw_file,
                                'id':true_ID,
                                'is_decoy':is_decoy,
                                'precursorMZ':true_precursorMZs,    
                                'pepmass':true_pepmasses,
                                'charge':true_charges,
                                'peptide':true_peptides,
                                'topk_peptides':predicted_peptides,
                                'topk_distances':predicted_distances,
                                'mzs':true_mzs,
                                'intensities':true_intensities,                               
                                })

    if not os.path.exists(OUTPUT_DIR):
        os.mkdir(OUTPUT_DIR)

    #search_results.to_csv(os.path.join(OUTPUT_DIR,'search_results.csv'),index=False)
    #search_results.to_hdf(os.path.join(OUTPUT_DIR,'search_results.h5'),key='search_results', mode='w')
    with pd.HDFStore(os.path.join(OUTPUT_DIR,'search_results.h5')) as store:
        store.put(raw_file,search_results)
    # if os.path.exists(os.path.join(OUTPUT_DIR,'search_results.h5')):
    #     prev=pd.read_hdf(os.path.join(OUTPUT_DIR,'search_results.h5'),'search_results')      
    #     search_results = pd.concat([prev,search_results],ignore_index=True)
    #     search_results.to_hdf(os.path.join(OUTPUT_DIR,'search_results.h5'),key='search_results', mode='w')
    # else:
    #     search_results.to_hdf(os.path.join(OUTPUT_DIR,'search_results.h5'),key='search_results', mode='w')
    ####### SEARCH RESULTS DATAFRAME #######
    ######################################