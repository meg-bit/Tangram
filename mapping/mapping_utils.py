"""
    Mapping helpers
"""

import numpy as np
import pandas as pd
import scanpy as sc
import torch
import logging

from scipy.sparse.csc import csc_matrix
from scipy.sparse.csr import csr_matrix

from . import mapping_optimizer as mo


def pp_adatas(adata_1, adata_2, genes=None):
    """
    Pre-process AnnDatas so that they can be mapped. Specifically:
    - Subset the AnnDatas to `genes` (non-shared genes are removed).
    - Re-order genes in `adata_2` so that they are consistent with those in `adata_1`.
    - Ensures `X` is in `numpy.ndarray` format.
    :param adata_1:
    :param adata_2:
    :param genes:
    List of genes to use. If `None`, all genes are used.
    :return:
    """
    if genes is None:
        # Use all genes
        genes = adata_1.var.index.values
    else:
        genes = list(genes)

    # Refine `marker_genes` so that they are shared by both adatas
    mask = adata_1.var.index.isin(genes)
    genes = adata_1.var[mask].index.values
    mask = adata_2.var.index.isin(genes)
    genes = adata_2.var[mask].index.values
    logging.info(f'{len(genes)} marker genes shared by AnnDatas.')

    # Subset adatas on marker genes
    mask = adata_1.var.index.isin(genes)
    adata_1 = adata_1[:, mask]
    mask = adata_2.var.index.isin(genes)
    adata_2 = adata_2[:, mask]
    assert adata_2.n_vars == adata_1.n_vars

    # re-order spatial adata to match gene order in single cell adata
    adata_2 = adata_2[:, adata_1.var.index.values]
    assert adata_2.var.index.equals(adata_1.var.index)

    # cast expression matrices to numpy
    for adata in [adata_1, adata_2]:
        if ~isinstance(adata.X, np.ndarray):
            adata.X = adata.X.toarray()
    return adata_1, adata_2


def map_cells_to_space(adata_cells, adata_space, mode='simple', adata_map=None,
                      device='cuda:0', learning_rate=0.1, num_epochs=1000):
    """
        Map single cell data (`adata_1`) on spatial data (`adata_2`). If `adata_map`
        is provided, resume from previous mapping.
        Returns a cell-by-spot AnnData containing the probability of mapping cell i on spot j.
        The `uns` field of the returned AnnData contains the training genes.
    """
    
    logging.info('Allocate tensors for mapping.')

    # AnnData matrix can be sparse or not
    if isinstance(adata_cells.X, csc_matrix) or isinstance(adata_cells.X, csr_matrix):
        S = np.array(adata_cells.X.toarray(), dtype='float32')
    elif isinstance(adata_cells.X, np.ndarray):
        S = np.array(adata_cells.X, dtype='float32')
    else:
        X_type = type(adata_cells.X)
        logging.error('AnnData X has unrecognized type: {}'.format(X_type))
        raise NotImplementedError
    
    if isinstance(adata_space.X, csc_matrix) or isinstance(adata_space.X, csr_matrix):
        G = np.array(adata_space.X.toarray(), dtype='float32')
    elif isinstance(adata_space.X, np.ndarray):
        G = np.array(adata_space.X, dtype='float32')
    else:
        X_type = type(adata_space.X)
        logging.error('AnnData X has unrecognized type: {}'.format(X_type))
        raise NotImplementedError

    d = np.zeros(adata_space.n_obs)
    device = torch.device(device)  # for gpu

    if mode == 'simple':
        hyperparameters = {
            'lambda_d': 0,  # KL (ie density) term
            'lambda_g1': 1,  # gene-voxel cos sim
            'lambda_g2': 0,  # voxel-gene cos sim
            'lambda_r': 0,  # regularizer: penalize entropy
        }
    else:
        raise NotImplementedError

    mapper = mo.Mapper(
        S=S, G=G, d=d, device=device, adata_map=adata_map,
        **hyperparameters,
    )

    logging.info('Begin training...')
    # TODO `train` should return the loss function
    mapping_matrix = mapper.train(
        learning_rate=learning_rate,
        num_epochs=num_epochs
    )

    logging.info('Saving results..')
    adata_map = sc.AnnData(X=mapping_matrix,
                           obs=adata_cells.obs.copy(),
                           var=adata_space.obs.copy())

    # Build cosine similarity for each training gene
    G_predicted = (adata_map.X.T @ S)
    cos_sims = []
    for v1, v2 in zip(G.T, G_predicted.T):
        norm_sq = np.linalg.norm(v1) * np.linalg.norm(v2)
        cos_sims.append((v1 @ v2) / norm_sq)
    training_genes = list(np.reshape(adata_cells.var.index.values, (-1,)))
    df_cs = pd.DataFrame(cos_sims, training_genes, columns=['score'])
    df_cs = df_cs.sort_values(by='score', ascending=False)
    adata_map.uns['train_genes_scores'] = df_cs

    return adata_map



