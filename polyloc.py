import numpy as np
import pandas as pd
import os
import sys
import time
import logging
from tqdm import tqdm
from polyfun import PolyFun, configure_logger, get_file_name, SNP_COLUMNS


def splash_screen():
    print('*********************************************************************')
    print('* PolyLoc (POLYgenic LOCalization of complex trait heritability')
    print('* Version 1.0.0')
    print('* (C) 2019 Omer Weissbrod')
    print('*********************************************************************')
    print()
    
    
def check_args(args):
    
    #verify that the requested computations are valid
    mode_params = np.array([args.compute_partitions, args.compute_ldscores, args.compute_polyloc])
    if np.sum(mode_params)==0:
        raise ValueError('must specify at least one of --compute-partitions, --compute-ldscores, --compute-polyloc')
    if args.compute_partitions and args.compute_polyloc and not args.compute_ldscores:
        raise ValueError('cannot use both --compute-partitions and --compute_polyloc without also specifying --compute-ldscores')
    if args.chr is not None:
        if args.compute_partitions or args.compute_polyloc:
            raise ValueError('--chr can only be specified when using only --compute-ldscores')
    if args.bfile_chr is not None:
        if not args.compute_ldscores and not args.compute_partitions:
            raise ValueError('--bfile-chr can only be specified when using --compute-partitions or --compute-ldscores')
    if args.compute_ldscores and args.compute_polyloc and not args.compute_partitions:
        raise ValueError('cannot use both --compute-ldscores and --compute_polyloc without also specifying --compute-partitions')    
        
    if args.posterior is not None and not args.compute_partitions:
        raise ValueError('--posterior can only be specified together with --compute-partitions')        
    if args.sumstats is not None and not args.compute_polyloc:
        raise ValueError('--sumstats can only be specified together with --compute-polyloc')
    
    #verify partitioning parameters
    if args.skip_Ckmedian and (args.num_bins is None or args.num_bins<=0):
        raise ValueError('You must specify --num-bins when using --skip-Ckmedian')        

    #partitionining-related parameters
    if args.compute_partitions:
        if args.bfile_chr is None:
            raise ValueError('You must specify --bfile-chr when you specify --compute-partitions')
        if args.posterior is None:
            raise ValueError('--posterior must be specified when using --compute-partitions')
            
    #verify LD-score related parameters
    if args.compute_ldscores:
        if args.bfile_chr is None:
            raise ValueError('You must specify --bfile-chr when you specify --compute-ldscores')    
        if args.ld_wind_cm is None and args.ld_wind_kb is None and args.ld_wind_snps is None:
            args.ld_wind_cm = 1.0
            logging.warning('no ld-wind argument specified.  PolyLoc will use --ld-cm 1.0')

    if not args.compute_ldscores:
        if not (args.ld_wind_cm is None and args.ld_wind_kb is None and args.ld_wind_snps is None):
            raise ValueError('--ld-wind parameters can only be specified together with --compute-ldscores')
        if args.keep is not None:
            raise ValueError('--keep can only be specified together with --compute-ldscores')
        if args.chr is not None:
            raise ValueError('--chr can only be specified together with --compute-ldscores')

    if args.compute_polyloc:
        if args.sumstats is None:
            raise ValueError('--sumstats must be specified when using --compute-polyloc')    
        if args.w_ld_chr is None:
            raise ValueError('--w-ld-chr must be specified when using --compute-polyloc')    
            
    return args

def check_files(args):

    if args.compute_partitions:
        if not os.path.exists(args.posterior):
            raise IOError('%s not found'%(args.posterior))
        
    #check that required input files exist
    if args.compute_ldscores or args.compute_partitions:
        if args.chr is None: chr_range = range(1,23)            
        else: chr_range = range(args.chr, args.chr+1)
        
        for chr_num in chr_range:
            get_file_name(args, 'bim', chr_num, verify_exists=True)
            get_file_name(args, 'fam', chr_num, verify_exists=True)
            get_file_name(args, 'bed', chr_num, verify_exists=True)
            if not args.compute_partitions:
                get_file_name(args, 'bins', chr_num, verify_exists=True)
                
    if args.compute_polyloc:    
        for chr_num in range(1,23):
            get_file_name(args, 'w-ld', chr_num, verify_exists=True)
            if not args.compute_partitions:
                get_file_name(args, 'bins', chr_num, verify_exists=True)
            
        

    

class PolyLoc(PolyFun):
    def __init__(self):
        pass
        
        
        
    def load_posterior_betas(self, args):
        if args.posterior.endswith('.parquet'):
            df_posterior = pd.read_parquet(args.posterior)
        else:
            df_posterior = pd.read_table(args.posterior, delim_whitespace=True)
            
        #preprocess columns
        df_posterior.columns = df_posterior.columns.str.upper()
        
        #make sure that all required columns are found
        has_missing_col = False
        for column in SNP_COLUMNS + ['BETA_MEAN', 'BETA_SD']:
            if column not in df_posterior.columns:
                logging.error('%s has a missing column: %s'%(args.posterior, column))
                has_missing_col = True
        if has_missing_col:
            raise ValueError('%s has missing columns'%(args.posterior))
            
        df_posterior['SNPVAR'] = df_posterior['BETA_MEAN']**2 + df_posterior['BETA_SD']**2
        self.df_snpvar = df_posterior
        
        
    def polyloc_partitions(self, args):
    
        self.load_posterior_betas(args)    
        self.partition_snps_to_bins(args, use_ridge=False)
        
        #add another partition for all SNPs not in the posterior file
        df_bim_list = []
        for chr_num in range(1,23):
            df_bim_chr = pd.read_table(args.bfile_chr+'%d.bim'%(chr_num), delim_whitespace=True, names=['CHR', 'SNP', 'CM', 'BP', 'A1', 'A2'])            
            df_bim_list.append(df_bim_chr)
        df_bim = pd.concat(df_bim_list, axis=0)
        df_bim.index = df_bim['SNP'] + df_bim['A1'] + df_bim['A2']
        self.df_bins.index = self.df_bins['SNP'] + self.df_bins['A1'] + self.df_bins['A2']
        
        #make sure that all variants in the posterior file are also in the plink files
        if np.any(~self.df_bins.index.isin(df_bim.index)):
            raise ValueError('Found variants in posterior file that are not found in the plink files')
            
        #add a new bin for SNPs that are not found in the posterior file (if there are any)
        if df_bim.shape[0] > self.df_bins.shape[0]:
            new_snps = df_bim.index[~df_bim.index.isin(self.df_bins.index)]
            df_bins_new = df_bim.loc[new_snps, SNP_COLUMNS].copy()
            for colname in self.df_bins.columns:
                df_bins_new[colname] = False
            new_colname = 'snpvar_bin%d'%(df_bins_new.shape[1] - len(SNP_COLUMNS)+1)
            self.df_bins[new_colname] = False
            df_bins_new[new_colname] = True
            self.df_bins = pd.concat([self.df_bins, df_bins_new], axis=0)
        
        #save the bins to disk
        self.save_bins_to_disk(args)
        
        #save the bin sizes to disk
        df_binsize = pd.DataFrame(index=np.arange(1,self.df_bins.shape[1] - len(SNP_COLUMNS)+1))
        df_binsize.index.name='BIN'
        df_binsize['BIN_SIZE'] = self.df_bins.drop(columns=SNP_COLUMNS).sum(axis=0).values
        df_binsize.to_csv(args.output_prefix+'.binsize', sep='\t', index=True)
        
        
        
    def compute_polyloc(self, args):
    
        #run S-LDSC and compute taus
        self.run_ldsc(args, use_ridge=False, nn=True, evenodd_split=False, keep_large=True)        
        hsqhat = self.hsqhat
        jknife = hsqhat.jknife
        taus = jknife.est[0, :hsqhat.n_annot] / hsqhat.Nbar
        
        #load bin sizes
        df_binsize = pd.read_table(args.output_prefix+'.binsize', sep='\t')
        
        #compute df_polyloc
        df_polyloc = df_binsize
        df_polyloc['%H2'] = taus * df_polyloc['BIN_SIZE']
        df_polyloc['%H2'] /= df_polyloc['%H2'].sum()
        df_polyloc['SUM_%H2'] = df_polyloc['%H2'].cumsum()
        
        #write df_polyloc to output file
        outfile = args.output_prefix+'.polyloc'
        df_polyloc.to_csv(args.output_prefix+'.polyloc', sep='\t', float_format='%0.5f', index=False)
        logging.info('Wrote output to %s'%(outfile))
        

    def polyloc_main(self, args):
    
        #compute snp variances using L2-regularized S-LDSC with an odd/even chromosome split
        if args.compute_partitions:
            self.polyloc_partitions(args)
            
        #compute LD-scores of SNP partitions
        if args.compute_ldscores:
            self.compute_ld_scores(args)
        
        #compute polygenic localization
        if args.compute_polyloc:
            self.compute_polyloc(args)
            
            
            
    
        
    
        


if __name__ == '__main__':

    import argparse
    parser = argparse.ArgumentParser()

    #partitioning-related parameters
    parser.add_argument('--num-bins', type=int, default=None, help='Number of bins to partition SNPs into. If not specified, PolyLoc will automatically select this number based on a BIC criterion')
    parser.add_argument('--skip-Ckmedian', default=False, action='store_true', help='If specified, use a regular K-means algorithm instead of the R Ckmeans.1d.dp package')
    
    #mode related parameters
    parser.add_argument('--compute-partitions', default=False, action='store_true', help='If specified, PolyLoc will compute per-SNP h2 using L2-regularized S-LDSC')
    parser.add_argument('--compute-ldscores', default=False, action='store_true', help='If specified, PolyLoc will compute LD-scores of SNP bins')
    parser.add_argument('--compute-polyloc', default=False, action='store_true', help='If specified, PolyLoc will perform polygenic localization of SNP heritability')
    
    #ld-score related parameters
    parser.add_argument('--chr', type=int, default=None, help='Chromosome number (only applicable when only specifying --ldscores). If not set, PolyLoc will compute LD-scores for all chromosomes')
    #parser.add_argument('--npz-prefix', default=None, help='Prefix of npz files that encode LD matrices (used to compute LD-scores)')
    parser.add_argument('--ld-wind-cm', type=float, default=None, help='window size to be used for estimating LD-scores in units of centiMorgans (cM).')
    parser.add_argument('--ld-wind-kb', type=int, default=None, help='window size to be used for estimating LD-scores in units of Kb.')
    parser.add_argument('--ld-wind-snps', type=int, default=None, help='window size to be used for estimating LD-scores in units of SNPs.')
    parser.add_argument('--chunk-size',  type=int, default=50, help='chunk size for LD-scores calculation')
    parser.add_argument('--keep',  default=None, help='File with ids of individuals to use when computing LD-scores')
    
    #data input/output parameters
    parser.add_argument('--sumstats', help='Input summary statistics file')
    parser.add_argument('--posterior', help='Input file with posterior means and variances of causal effect sizes')
    parser.add_argument('--w-ld-chr', help='Suffix of LD-score weights files (as in ldsc)')
    parser.add_argument('--bfile-chr', default=None, help='Prefix of plink files (used to compute LD-scores)')
    parser.add_argument('--output-prefix', required=True, help='Prefix of all PolyLoc out file namess')    
    
    #show splash screen
    splash_screen()

    #extract args
    args = parser.parse_args()
    
    #check that the output directory exists
    if os.path.isabs(args.output_prefix) and not os.path.exists(os.path.dirname(args.output_prefix)):
        raise ValueError('output directory %s doesn\'t exist'%(os.path.dirname(args.output_prefix)))
    
    #configure logger
    configure_logger(args.output_prefix)
        
    #check and fix args
    args = check_args(args)
    check_files(args)
    args.anno = None
    
    #create and run PolyLoc object
    polyloc_obj = PolyLoc()
    polyloc_obj.polyloc_main(args)
    
    print()
    