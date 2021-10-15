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
sys.path.append("../Neonomicon")
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
from tf_data_json import USIs,parse_json_npy
from usi_magic import parse_usi
from tf_data_mgf import MGF,parse_mgf_npy
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

#db_pepmasses = np.load(os.path.join(DB_DIR,"pepmasses.npy"))

#sorted_indices = np.argsort(db_pepmasses)

#db_embedded_peptides=db_embedded_peptides[sorted_indices]
#db_peptides=db_peptides[sorted_indices]
#db_pepmasses=db_pepmasses[sorted_indices]

DELTA_MASS = 200

if False:
    ####### MASS BUCKETS #######
    ######################################
    def bucket_indices(X,n_buckets=10):
        from sklearn.preprocessing import KBinsDiscretizer
        est = KBinsDiscretizer(n_bins=n_buckets, encode='ordinal', strategy='quantile')
        masses = np.expand_dims(X,axis=-1)
        mass_bucket_indices = np.squeeze(est.fit_transform(masses))
        #in_bucket_indices = mass_bucket_indices==bucket
        buckets = [np.arange(X.shape[0])[mass_bucket_indices==bucket] for bucket in range(n_buckets)]
        #print(np.bincount(mass_bucket_indices.astype(np.int32)))
        #print(est.bin_edges_)
        return buckets,est

    def get_lowest_highest_bucket(est,mass,delta_mass=DELTA_MASS):
        lowest,highest = np.squeeze(est.transform(np.expand_dims([mass-delta_mass,mass+delta_mass],axis=-1)))
        return int(lowest),int(highest)

    def get_space(mass,est,buckets):
        lowest, highest = get_lowest_highest_bucket(est,mass=mass)
        space = np.concatenate(buckets[lowest:highest+1])
        return space

    buckets,est = bucket_indices(db_pepmasses,100)

    space = get_space(mass=4014.23,est=est,buckets=buckets)

    ####### MASS BUCKETS #######
    ######################################

print('fire up datasets...')
#N = None
N = args.DEBUG_N
#N = 163410

if False:
    files = glob.glob(os.path.join(JSON_DIR,'*.json'))
    #files = glob.glob(os.path.join('../../scratch/USI_files/PXD007963/**/','*.json'))
    #files = glob.glob(os.path.join('../../scratch/USI_files/delete_me/PXD003916/Michelle-Experimental-Sample6.mzid_Michelle-Experimental-Sample6.MGF','*.json'))
    #files = glob.glob(os.path.join('../../Neonomicon/files/test/**/','*.json'))
    #files = glob.glob(os.path.join('../../Neonomicon/dump','*.json'))
    random.seed(0)
    random.shuffle(files)
    files = files[:N]

    ds = USIs(files,batch_size=1,buffer_size=1).get_dataset().unbatch()
    ds_spectra = ds.map(lambda x,y: x).batch(64)
    ds_peptides = ds.map(lambda x,y: y).batch(256)

if True:
    from pyteomics import mgf
    file = args.MGF#"/hpi/fs00/home/tom.altenburg/scratch/yHydra_testing/PXD007963/raw/qe2_03132014_11trcRBC-2.mgf"
    mgf_size = len(mgf.read(file))
    ds = MGF([file]).get_dataset().take(mgf_size).unbatch()
    ds_spectra = ds.map(lambda x,y: x).batch(BATCH_SIZE)
    ds = MGF([file]).get_dataset().take(mgf_size).unbatch()
    ds_scans = ds.map(lambda x,y: y).batch(1).as_numpy_iterator()

true_peptides = []
true_precursorMZs = []
true_pepmasses = []
true_charges = []
true_ID = []
true_mzs = []
true_intensities = []

input_specs_npy = {
    "mzs": np.float32,
    "intensities": np.float32,
    "usi": str,
    #"charge": float,
    "precursorMZ": float,
}

def parse_json_npy_(file_location): return parse_json_npy(file_location,specs=input_specs_npy)

if __name__ == '__main__':

    if True:
        if args.MGF is None:
            with multiprocessing.Pool(64) as p:
                print('getting true peptides...')
                for psm in tqdm(list(p.imap(parse_json_npy_, files,1))):
                #for psm in tqdm(list(map(lambda file_location: parse_json_npy(file_location,specs=input_specs_npy), files))):
                    if MSMS_OUTPUT_IN_RESULTS:
                        true_mzs.append(psm['mzs'])
                        true_intensities.append(psm['intensities'])
                    #charge=psm['charge']
                    precursorMZ=float(psm['precursorMZ'])
                    usi=str(psm['usi'])
                    collection_identifier, run_identifier, index, charge, peptideSequence, positions = parse_usi(usi)
                    true_peptides.append(peptideSequence)
                    true_precursorMZs.append(precursorMZ)
                    true_pepmasses.append(precursor2peptide_mass(precursorMZ,int(charge)))
                    true_charges.append(int(charge))
                    true_ID.append(usi)
        else:
            with multiprocessing.Pool(64) as p:
                print('getting scan information...')
                for i,spectrum in enumerate(tqdm(parse_mgf_npy(file))):

                    if MSMS_OUTPUT_IN_RESULTS:
                        true_mzs.append(spectrum['mzs'])
                        true_intensities.append(spectrum['intensities'])
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
            embedded_spectra = spectrum_embedder.predict(ds_spectra)
            #print('embedding peptides...')
            #embedded_peptides = sequence_embedder.predict(ds_peptides)

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
    print(list(map(len,buckets)))    

    db_narrow = add_bucket_adress(db,db_pepmasses,est)
    
    embedded_spectra_narrow = add_bucket_adress(embedded_spectra,true_pepmasses,est,0)

    ######### NARROW
    ######################################

    ######################################
    ######### OPEN
    buckets,est = bucket_indices(db_pepmasses,'uniform',N_BUCKETS_OPEN)
    print(list(map(len,buckets)))    

    db_open = add_bucket_adress(db,db_pepmasses,est)
    
    embedded_spectra_open_0 = add_bucket_adress(embedded_spectra,true_pepmasses,est,0)

    embedded_spectra_open_m1 = add_bucket_adress(embedded_spectra,true_pepmasses,est,-1)
    embedded_spectra_open_p1 = add_bucket_adress(embedded_spectra,true_pepmasses,est,+1)
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



    if False:
        with open("/mnt/data/crux_mock_search/comet.psms.json",'r') as f:
            true_peptides = json.load(f)

        from preprocessing import get_sequence_of_indices,trim_sequence
        peptide_indices = list(map(lambda x: trim_sequence(get_sequence_of_indices(x),MAX_PEPTIDE_LENGTH=42), true_peptides.values()))
        peptide_indices = np.reshape(peptide_indices,(-1,42))
        db = sequence_embedder.predict(peptide_indices,batch_size=4096)
        db_peptides = np.array(list(true_peptides.values()))

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

    search_results = pd.DataFrame({'id':true_ID,
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
    search_results.to_hdf(os.path.join(OUTPUT_DIR,'search_results.h5'),key='search_results', mode='w')
    exit()
    ####### SEARCH RESULTS DATAFRAME #######
    ######################################


    # I = []
    # for i,query_i in enumerate(query):
    #     query_i = np.expand_dims(query_i,0)
    #     true_pepmass = true_pepmasses[i]
    #     space = get_space(true_pepmass,est=est,buckets=buckets)
    #     index = get_index(db[space],k=k,metric='euclidean',method='faiss',use_gpu=False)
    #     I_i = perform_search(query=query_i,k=k,index=index,method='faiss')
    #     I_i = space[I_i]
    #     I.append(I_i)
    # I = np.array(I)
    # I = np.reshape(I,(N,k))

    #scans = np.array(list(ds_scans)).flatten()

    k_accuracy=[]
    identified_peptides = []
    identified_peptides_in_topk = []

    for i,k50 in tqdm(enumerate(db_peptides[I])):
        #scan = str(scans[i])
        #print(i,k50)
        try: 
            #identified_peptide = true_peptides[scan]
            identified_peptide = true_peptides[i]
            identified_peptides.append(identified_peptide)
            #print(set(k50))
            #print(set([true_peptides[scan]]))
            intersection = set(k50).intersection(set([identified_peptide]))
            if len(intersection) > 0:
                identified_peptides_in_topk.append(identified_peptide)
                k_accuracy.append(1)
            else:
                k_accuracy.append(0)

        except:
            k_accuracy.append(-1)
            #print("not_identified")

    k_accuracy = np.array(k_accuracy)

    print('accuracy 0:',sum(k_accuracy==0))
    print('accuracy 1:',sum(k_accuracy==1))
    print('accuracy-1:',sum(k_accuracy==-1))

    result_peptides_set = set(db_peptides[I].flatten().tolist())

    in_db = set(true_peptides).intersection(set(db_peptides))
    intersection_all = result_peptides_set.intersection(set(true_peptides))
    intersection_searched = result_peptides_set.intersection(set(identified_peptides))


    print(len(in_db))
    print(len(intersection_all))
    print(len(intersection_searched))
    print(len(set(identified_peptides)))
    print(len(set(identified_peptides_in_topk)))

    # for i,embedded_peptide in enumerate(query):
    #     k_neighbours = db_embedded_peptides[I][i].tolist()
    #     k_neighbours = [tuple(x) for x in k_neighbours]
    #     print(tuple(embedded_peptide.tolist()) in set(k_neighbours))
