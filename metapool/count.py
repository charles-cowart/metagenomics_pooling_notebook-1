import re
import pandas as pd
import warnings
from os.path import join, abspath, basename, exists
from glob import glob
from json import load
from skbio.io import read as skb_read


# first group is the sample name from the sample sheet, second group is the
# cell number, third group is the lane number, and fourth group is the
# forward/reverse/index id.
#
# Here's a few examples for this regular expression: tinyurl.com/filenamepatt
import metapool

SAMPLE_PATTERN = re.compile(r'(.*)_(S\d{1,4})_(L\d{1,3})_([RI][12]).*')

# first group is the number of sequences written to fwd and reverse files
# Here's a few examples for this regular expression: tinyurl.com/samtoolspatt
SAMTOOLS_PATTERN = re.compile(r'\[.*\] processed (\d+) reads',
                              flags=re.MULTILINE)


def _extract_name_and_lane(filename):
    search = re.match(SAMPLE_PATTERN, filename)
    if search is None:
        raise ValueError(f'Unrecognized filename pattern {filename}')

    name, _, lane, _ = search.groups()

    # remove the leading L and any leading zeroes
    lane = lane[1:].lstrip('0')
    return name, lane


def _parse_fastp_counts(path):
    with open(path) as fp:
        stats = load(fp)

        # check all the required keys are present, otherwise the file could be
        # malformed and we would only see a weird KeyError exception
        if ('summary' not in stats or
            'after_filtering' not in stats['summary'] or
           'total_reads' not in stats['summary']['after_filtering']):
            raise ValueError(f'The fastp log for {path} is malformed')

        return int(stats['summary']['after_filtering']['total_reads'])


def _parse_samtools_counts(path):
    with open(path, 'r') as f:
        matches = re.match(SAMTOOLS_PATTERN, f.read())

        if matches is None:
            raise ValueError(f'The samtools log for {path} is malformed')

        # divided by 2 because samtools outputs the number of records found
        # in the forward and reverse files
        return int(matches.groups()[0]) / 2.0


def _parsefier(run_dir, metadata, subdir, suffix, name, funk):
    """High order helper to search through a run directory

    Parameters
    ----------
    run_dir: str
        Illumina's run directory.
    metadata: metapool.KLSampleSheet or mapping-file (pd.DataFrame)
        Sample sheet for the samples to get counts for.
    subdir: str
        Name of the directory in the project folder.
    suffix: str
        Suffix of the log files.
    name: str
        Column name for the output counts.
    funk: callable
        Function to parse log files.

    Returns
    -------
    pd.DataFrame
        Table with sample, lane and counts.
    """
    out = []

    if isinstance(metadata, metapool.KLSampleSheet):
        projects = {(s.Sample_Project, s.Lane) for s in metadata}
        expected = {s.Sample_ID for s in metadata}
    else:
        raise ValueError("counts not implemented for amplicon")

    for project, lane in projects:
        lane = lane.zfill(3)

        # log files are named after the sequence files themselves,
        # specifically the forward sequences, so we just make sure we
        # match the right lane, the forward file and the suffix. The
        # suffix is something like ".log" or "*.json" if you expect to see
        # other characters before the extension.
        logs = glob(join(run_dir, project, subdir,
                         f'*_L{lane}_R1_001' + suffix))

        for log in logs:
            out.append([*_extract_name_and_lane(basename(log)),
                        project, log])

    out = pd.DataFrame(
        columns=['Sample_ID', 'Lane', 'Sample_Project', 'path'],
        data=out)

    found = set(out['Sample_ID'])

    # ignore the things not present in the sheet
    out = out[out['Sample_ID'].isin(expected)]

    dups = out.duplicated(subset=['Sample_ID', 'Lane'])
    if dups.any():
        pairs = [f"{r['Sample_ID']} in lane {r['Lane']}"
                 for _, r in out[dups].iterrows()]

        # when running bcl2fastq/bclconvert multiple times you can run
        # into situations where the cell number is the only thing that
        # changes. For those situations, make sure you flag this as a
        # possible error
        raise ValueError('Multiple matches found for the same samples in'
                         ' the same lane, only one match is expected: %s' %
                         ', '.join(pairs))

    if expected > found:
        warnings.warn(f'No {name} log found for these samples: %s' %
                      ', '.join(expected - found))

    # quality_filtered_reads, non_host_reads, and the like are added as
    # columns to the output dataframe here.
    out[name] = out.path.apply(funk).astype('float64')

    # drop columns that are no longer needed from the output.
    out.drop(columns=['path', 'Sample_Project'], inplace=True)

    out.set_index(['Sample_ID', 'Lane'], inplace=True,
                  verify_integrity=True)

    return out


def _safe_get(_document, _key):
    """Prevent generic KeyError exceptions"""
    if _key not in _document:
        raise KeyError(f'bcl stats file is missing {_key} attribute')
    else:
        return _document[_key]


def bcl2fastq_counts(run_dir, sample_sheet):
    bcl2fastq_path = join(abspath(run_dir), 'Stats/Stats.json')
    bclconvert_path = join(abspath(run_dir), 'Reports/Demultiplex_Stats.csv')

    if exists(bcl2fastq_path):
        if exists(bclconvert_path):
            raise IOError(f"both '{bcl2fastq_path}' and '{bclconvert_path}'"
                          " exist")
        else:
            return _bcl2fastq_counts(bcl2fastq_path)
    elif exists(bclconvert_path):
        return _bclconvert_counts(bclconvert_path)
    else:
        raise IOError(f"Cannot find Stats.json '{bcl2fastq_path}' or "
                      f"Demultiplex_Stats.csv '{bclconvert_path}' for this"
                      " run")


def _bcl2fastq_counts(path):
    with open(path) as fp:
        contents = load(fp)

    out = []
    for lane in _safe_get(contents, 'ConversionResults'):
        table = pd.DataFrame(_safe_get(lane, 'DemuxResults'))
        table['Lane'] = str(_safe_get(lane, 'LaneNumber'))

        out.append(table)

    out = pd.concat(out)
    out.rename(columns={'SampleId': 'Sample_ID',
                        'NumberReads': 'raw_reads_r1r2'},
               inplace=True)
    out = out[['Sample_ID', 'Lane', 'raw_reads_r1r2']]
    out.set_index(['Sample_ID', 'Lane'], inplace=True, verify_integrity=True)
    return out


def _bclconvert_counts(path):
    # read the csv in from file
    df = pd.read_csv(path)
    # subselect only the columns we're concerned with
    df = df[["SampleID", "Lane", "# Reads"]]

    # double # Reads to represent forward and reverse reads.
    df['raw_reads_r1r2'] = df['# Reads'] * 2
    df.drop('# Reads', axis=1, inplace=True)

    # filter out rows that reference an 'Undetermined' fastq.gz file
    # and create our own copy to return to the user
    df = df.loc[df['SampleID'] != 'Undetermined'].copy()
    # rename columns to standard values for metapool
    df.rename(columns={'SampleID': 'Sample_ID'}, inplace=True)
    # create indexes on these columns
    df['Lane'] = df['Lane'].astype(str)
    df.set_index(['Sample_ID', 'Lane'], inplace=True, verify_integrity=True)

    return df


def fastp_counts(run_dir, metadata):
    # total_biological_reads_r1r2 represents # of reads after adapter-trimming,
    # but before any manner of pangenome-filtering. # fastp's 'after-filtering'
    # results match these values, rather than post all filtering.
    return _parsefier(run_dir, metadata, 'json', '.json',
                      'total_biological_reads_r1r2',
                      _parse_fastp_counts)


def minimap2_counts(run_dir, metadata):
    return _parsefier(run_dir, metadata, 'samtools', '.log',
                      'non_host_reads', _parse_samtools_counts)


def count_sequences_in_fastq(file_path):
    variants = ['illumina1.8', 'illumina1.3', 'sanger']

    for variant in variants:
        try:
            # see https://scikit.bio/docs/latest/generated/skbio.io.format.
            # fastq.html#quality-score-variants for more information on
            # variants. Variant is needed to correctly interpret a sequence's
            # quality range. We don't explicitly need quality-ranges for this
            # application but we unfortunately we cannot omit it.
            sequences = skb_read(file_path, format='fastq',
                                 variant=variant)
            return sum(1 for sequence in sequences)
        except ValueError:
            # assume ValueError is because the correct variant wasn't
            # specified.
            pass

    raise ValueError(f"'{file_path} could not be opened")


def direct_sequence_counts(run_dir, metadata):
    if isinstance(metadata, metapool.KLSampleSheet):
        projects = {(s.Sample_Project, s.Lane) for s in metadata}
        expected = {s.Sample_ID for s in metadata}
    else:
        raise ValueError("counts not implemented for amplicon")

    sample_ids = []
    lanes = []
    counts = []

    for project, lane in projects:
        if join(run_dir, 'filtered_sequences'):
            subdir = join(run_dir, project, 'filtered_sequences')
        elif join(run_dir, 'trimmed_sequences'):
            subdir = join(run_dir, project, 'trimmed_sequences')
        else:
            raise ValueError("'filtered_sequences', 'trimmed_sequences' "
                             "directories do not exist")

        fp = join(subdir, '*_L%s_R?_001.trimmed.fastq.gz' % lane.zfill(3))

        # glob is going to return both _R1_ and _R2_ fastqs. sorted() will
        # pair them such that we can iterate through them as a pair.
        fp_iter = iter(sorted(glob(fp)))
        for r1, r2 in zip(fp_iter, fp_iter):
            reg = r"^(.*)_S\d+_L\d\d\d_R\d_\d\d\d.trimmed.fastq.gz$"

            m_r1 = re.match(reg, basename(r1))
            m_r2 = re.match(reg, basename(r2))

            if m_r1 is None or m_r2 is None:
                raise ValueError(f"a sample-id could not be extracted from "
                                 f"{r1} or {r2}")

            if m_r1[1] != m_r2[1]:
                raise ValueError("a matching sample-id could not be "
                                 f"extracted from {r1} and {r2}")

            sample_id = m_r1[1]

            r1_counts = count_sequences_in_fastq(r1)
            r2_counts = count_sequences_in_fastq(r2)

            sample_ids.append(sample_id)
            lanes.append(lane)
            counts.append(float(r1_counts + r2_counts))

    df = pd.DataFrame(list(zip(sample_ids, lanes, counts)),
                      columns=['Sample_ID', 'Lane',
                               'quality_filtered_reads_r1r2'])

    df['Lane'] = df['Lane'].astype(str)
    df.set_index(['Sample_ID', 'Lane'], inplace=True, verify_integrity=True)

    return df


def run_counts(run_dir, metadata):
    '''
    Generate and aggregate run-counts from multiple sources
    :param run_dir: A run-directory
    :param metadata: A sample-sheet (metagenomic) or mapping-file (amplicon)
    :return: pandas.DataFrame
    '''
    out = bcl2fastq_counts(run_dir, metadata).join(
        [fastp_counts(run_dir, metadata), minimap2_counts(run_dir, metadata),
         direct_sequence_counts(run_dir, metadata)])

    # convenience columns to assess sample quality
    ratio = out['quality_filtered_reads_r1r2'] / out['raw_reads_r1r2']
    out['fraction_passing_quality_filter'] = ratio
    out['fraction_non_human'] = (out['non_host_reads'] /
                                 out['quality_filtered_reads_r1r2'])

    out.fillna(value='NA', inplace=True)

    return out
