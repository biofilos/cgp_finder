import os
from copy import copy
import time
import sys
import pandas as pd
import numpy as np
from Bio import Entrez, SeqIO
from concurrent import futures
Entrez.email = 'user@mail.com'

GENOME_DB = "genomes.gb"
CPUS = int(sys.argv[1])

pd.options.mode.chained_assignment = None


def parse_gb(record):
    """
    Parse GenBank annotation files and returns a list of annotation features
    for each CDS with its chromosome, species and coordinates information
    :return: list of features
    """
    # records = (x for x in SeqIO.parse(GENOME_DB, 'genbank'))
    features_list = []
    # for record in SeqIO.parse(GENOME_DB, 'genbank'):
    sp = record.annotations['organism']
    for feat in record.features:
        if feat.type == 'source':
            # Try/except was here
            if 'chromosome' in feat.qualifiers.keys():
                chromosome_feat = feat.qualifiers['chromosome'][0]
            elif 'organelle' in feat.qualifiers.keys() and feat.qualifiers['organelle'][0] == 'mitochondrion':
                chromosome_feat = 'mitochondrion'
            else:
                chromosome_feat = record.annotations['accessions'][0]
        elif feat.type == 'CDS' and 'protein_id' in feat.qualifiers.keys():
            start_feat = feat.location.start.position
            end_feat = feat.location.end.position
            strand_feat = feat.location.strand
            if strand_feat not in [1, -1]:
                # Skip gene that has no strand information
                continue
            acc_feat = feat.qualifiers['protein_id'][0]
            try:
                symbol_feat = feat.qualifiers['gene'][0]
            except KeyError:
                symbol_feat = acc_feat
            symbol_feat = symbol_feat.replace(' ', '--')
            # Try/except was here
            if 'product' in feat.qualifiers.keys():
                name = feat.qualifiers['product'][0]
            else:
                if 'exception' in feat.qualifiers.keys():
                    name = feat.qualifiers['exception'][0]
                elif 'gene' in feat.qualifiers.keys():
                    name = feat.qualifiers['gene'][0]
                else:
                    name = acc_feat
            features_list.append([start_feat, end_feat, strand_feat, acc_feat,
                                  chromosome_feat, sp, symbol_feat, name])
    feat_table = pd.DataFrame(features_list, columns=['start', 'end', 'strand', 'acc', 'chromosome',
                                                      'species', 'symbol', 'name']).set_index('acc')
    feat_table['length'] = abs(feat_table['start'] - feat_table['end'])
    return feat_table


def get_sources(gb):
    for record in SeqIO.parse(gb, 'genbank'):
        for feature in record.features:
            if feature.type == 'source':
                yield record


def get_assembly_info(record):
    source = [feature for feature in record.features if feature.type == 'source']
    if len(source) > 0:
        feat = source[0]
        spp = record.annotations['organism']
        gi = record.annotations['gi']
        assembly_record = Entrez.read(Entrez.elink(dbfrom='nucleotide', db='assembly',
                                                   id=gi, rettype='gb'))
        assembly = assembly_record[0]['LinkSetDb'][0]['Link'][0]['Id']
        accession = copy(record.id)
        if 'chromosome' in feat.qualifiers.keys():
            chrom = feat.qualifiers['chromosome'][0]
        else:
            chrom = accession
        taxid = feat.qualifiers['db_xref'][0].split(":")[1]
        length = feat.location.end.real
        annotations = [spp, chrom, taxid, gi, accession, assembly, length]
        assembly = annotations
        return assembly


def sp_loop(in_table, columns):
    """
    Filters a table by a number of columns
    :param in_table: Input table
    :param columns: Column names to perform the filter
    :return: list of tables for each combination of column names
    """
    # Initialize the list of tables
    list_results = []
    for col_set in np.unique(in_table.set_index(columns).index):
        filtered_table = in_table
        cols_to_filter = {col_name: col_value for col_name, col_value in zip(columns, col_set)}
        for var in cols_to_filter.keys():
            filtered_table = filtered_table.loc[filtered_table[var] == cols_to_filter[var]]
        list_results.append(filtered_table)
    return list_results


def remove_overlaps(chrom_only):
    """
    Removes overlapping genes according to these rules:
    If the second gene (sorted by start coordinate) starts before the gene before ends
    AND both genes are in the same strand
    :param chrom_only: dataframe for each chromosome on a species
    :return: Dataframe
    """
    # Remove duplicated accession numbers
    chrom_only.reset_index(inplace=True)
    chrom_only.drop_duplicates('acc', inplace=True)
    chrom_only.set_index('acc', inplace=True)

    first_to_one_to_last = chrom_only.index.values[:-1]
    second_to_last = chrom_only.index.values[1:]

    overlapping = chrom_only.loc[second_to_last, 'start'] < chrom_only.loc[first_to_one_to_last, 'end'].values

    same_strand = chrom_only.loc[second_to_last, 'strand'] == chrom_only.loc[first_to_one_to_last, 'strand'].values

    for_removal = same_strand & overlapping
    for_removal_indices = for_removal.loc[for_removal].index
    non_overlapping_table = chrom_only.drop(for_removal_indices)
    return non_overlapping_table


def remove_overlaps_sp(sp):
    """
    Loads a csv named sp.csv in the current directory.
    Adds a length column
    Removes genes with duplicated start coordinates.
    :param sp:
    :return:
    """
    sp_table_list = []
    # Load species table
    sp_table = out_data.loc[out_data.species == sp]

    # Set indices so that a data frame per chromosome with ordered genes by position and length can be easily selected
    sp_table.sort_values(["chromosome", 'start', 'length'], inplace=True)

    # Remove entries that have the same start position and leave the longest one
    sp_table.drop_duplicates('start', inplace=True)

    # Remove overlapping genes
    # Get list of tables per chromosome
    chrom_tables = sp_loop(sp_table, ['species', 'chromosome'])
    # Get list of tables (one per chromosome) without overlapping genes
    non_overlapping = []
    for chrom_table in chrom_tables:
        before = chrom_table.shape[0]
        no_overlaps = remove_overlaps(chrom_table)
        after = no_overlaps.shape[0]
        while before != after:
            before = no_overlaps.shape[0]
            no_overlaps = remove_overlaps(no_overlaps)
            after = no_overlaps.shape[0]
        non_overlapping.append(no_overlaps)
    parsed_sp_table = pd.concat(non_overlapping)

    return parsed_sp_table


def dict_seq(fasta):
    seq_dict = {}
    for line in open(fasta):
        line = line.strip('\n')
        if line.startswith('>'):
            if "|" not in line:
                name = line.strip('>').split(' ')[0]
            else:
                name = line.strip('>').split('|')[3]
            seq_dict[name] = ''
        else:
            seq_dict[name] += line
    return seq_dict


def seq_column(accession):
    spp = all_sp_table.loc[accession, 'species']
    chromosome = all_sp_table.loc[accession, 'chromosome']
    try:
        seq = seq_dic[accession]
    except KeyError:
        try:
            print("Downloading {0} from the NCBI ({1} ch: {2})".format(accession, spp, chromosome))
            seq = str(SeqIO.read(Entrez.efetch(db='protein', id=accession, rettype='fasta'), 'fasta').seq)
        except:
            time.sleep(1)
            print("Downloading {} from the NCBI again".format(accession))
            seq = str(SeqIO.read(Entrez.efetch(db='protein', id=accession, rettype='fasta'), 'fasta').seq)
        record = pd.Series({accession: seq})
    return record


# Parse Genbank
if not os.path.exists('genes_cds.csv'):
    with futures.ProcessPoolExecutor(CPUS) as pool:
        out_data_lst = pool.map(parse_gb, SeqIO.parse(GENOME_DB, 'genbank'))
    out_data = pd.concat(out_data_lst)
    out_data = out_data.loc[~(out_data['name'].map(lambda x: 'isoform X' in x))]
    out_data = out_data.loc[~(out_data['chromosome'].isin(['mitochondrion', 'unknown', 'Unknown']))]
    # Save full annotation
    out_data['species'] = out_data['species'].map(lambda x: x.replace(' ', '_'))
    out_data.to_csv('genes_cds.csv')
    # Remove some columns
    mini = out_data.loc[:, ['species', 'chromosome', 'symbol', 'start', 'end', 'length', 'strand']]
    mini.to_csv('mini_table.csv')
    del mini
else:
    out_data = pd.read_csv('genes_cds.csv')


with futures.ProcessPoolExecutor(CPUS) as pool:
    sp_tables = pool.map(remove_overlaps_sp, list(set(out_data.species.values)))
all_sp_table = pd.concat(list(sp_tables))
all_sp_table.to_csv('genes_parsed.csv')

print("GenBank annotation parsed")

# Generate fasta file
genes_selected = all_sp_table
genes_selected.reset_index(inplace=True)


seq_dic = dict_seq('proteomes.fa')
print("Sequence dictionary, generated")

print("Sequence file, parsed")
if not os.path.exists("all_seqs.fa"):
    with open('all_seqs.fa', 'w') as seq_db:
        for index in genes_selected.index:
            seq_table = genes_selected.loc[index]
            spp = seq_table['species'].replace(' ', '_')
            chromosome = seq_table['chromosome']
            accession = seq_table['acc']
            symbol = seq_table['symbol']
            start = seq_table['start']
            end = seq_table['end']
            strand = seq_table['strand']
            try:
                seq = seq_dic[accession]
                del seq_dic[accession]
            except:
                try:
                    print("Downloading {0} from the NCBI ({1} ch: {2})".format(accession, spp, chromosome))
                    seq = str(SeqIO.read(Entrez.efetch(db='protein', id=accession, rettype='fasta'), 'fasta').seq)
                except:
                    time.sleep(1)
                    print("Downloading {} from the NCBI again".format(accession))
                    seq = str(SeqIO.read(Entrez.efetch(db='protein', id=accession, rettype='fasta'), 'fasta').seq)
            structure = '>{0}|{1}|{2}|{3}|{4}|{5}|{6}\n{7}\n'.format(spp, chromosome, accession,
                                                                     symbol, start, end, strand, seq)
            seq_db.write(structure)
            seq_db.flush()
    seq_db.close()

print("Sequence file, generated")
del seq_dic
del all_sp_table
del genes_selected
# rec_list = (x for x in SeqIO.parse(GENOME_DB, 'genbank') if )

# Assembly table
chrom_dict, annotations = {}, []
for record in SeqIO.parse(GENOME_DB, 'gb'):
    record.seq = ''
    source = [feature for feature in record.features if feature.type == 'source']
    if len(source) > 0:
        feat = source[0]
        length = feat.location.end.real
        spp = record.annotations['organism']
        accession = record.id
        if 'chromosome' in feat.qualifiers.keys():
            chrom = feat.qualifiers['chromosome'][0]
        else:
            chrom = accession
        if spp not in chrom_dict:
            chrom_dict[spp] = {}
        if chrom not in chrom_dict[spp]:
            print("initializing {}: {}".format(spp, chrom))
            chrom_dict[spp][chrom] = {'length': 0, 'annotation': None}

        if length > chrom_dict[spp][chrom]['length']:
            print(length, chrom_dict[spp][chrom])
            chrom_dict[spp][chrom]['length'] = length
            gi = record.annotations['gi']
            # print(gi)
            try:
                assembly_record = Entrez.read(Entrez.elink(dbfrom='nucleotide', db='assembly',
                                                           id=gi, rettype='gb'))
                assembly = assembly_record[0]['LinkSetDb'][0]['Link'][0]['Id']
            except RuntimeError:
                print("Trying to fetch assembly number again ({}, {})".format(spp,chrom))
                assembly_record = Entrez.read(Entrez.elink(dbfrom='nucleotide', db='assembly',
                                                           id=gi, rettype='gb'))
                assembly = assembly_record[0]['LinkSetDb'][0]['Link'][0]['Id']
            taxid = feat.qualifiers['db_xref'][0].split(":")[1]

            chrom_dict[spp][chrom]['annotation'] = [spp, chrom, taxid, gi, accession, assembly, length]

for sp in chrom_dict:
    for chrom in chrom_dict[sp]:
        annotations.append(chrom_dict[sp][chrom]['annotation'])

assembly_table = pd.DataFrame(list(annotations), columns=['species', 'chromosome', 'taxid',
                                                          'GI', 'chr_acc', 'Assembly', 'length'])
assembly_table['species'] = assembly_table['species'].map(lambda x: x.replace(' ', '_'))

assembly_table.to_csv('chromosomes.csv')
print("Assembly file, generated")

print("DONE")

