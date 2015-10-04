'''
Using characterised SVs, count normal and supporting reads at SV locations
'''
import warnings
import os
import numpy as np
import pysam
import csv
from collections import OrderedDict
from operator import methodcaller
import vcf
from . import parameters as params
from . import bamtools
from . import svDetectFuncs as svd

#TODO: remove after testing
import ipdb

def read_to_array(x,bamf):
    chrom = bamf.getrname(x.reference_id)
    try:
        read = np.array((x.query_name,chrom,x.reference_start,x.reference_end,x.query_alignment_start,
                         x.query_alignment_end,x.query_length,x.tlen,np.bool(x.is_reverse)),dtype=params.read_dtype)
        return read
    except TypeError:
        print 'Warning: record %s contains invalid attributes, skipping' % x.query_name
        #return np.empty(len(params.read_dtype),dtype=params.read_dtype)
        return np.empty(0)

def is_soft_clipped(r):
    return r['align_start'] != 0 or (r['len'] + r['ref_start'] != r['ref_end'])

def is_minor_softclip(r,threshold=params.tr):
    return (r['align_start'] < threshold) and ((r['align_end'] + r['ref_start'] - r['ref_end']) < threshold)

def is_normal_across_break(r,pos,min_ins,max_ins):
    # must overhang break by at least soft-clip threshold
    return  (not is_soft_clipped(r)) and \
            (abs(r['ins_len']) < max_ins and abs(r['ins_len']) > min_ins) and \
            (r['ref_start'] <= (pos - params.norm_overlap)) and \
            (r['ref_end'] >= (pos + params.norm_overlap)) 

def get_normal_overlap_bases(r,pos):
    return min( [abs(r['ref_start']-pos), abs(r['ref_end']-pos)] )

def is_normal_spanning(r,m,pos,min_ins,max_ins):
    if not (is_soft_clipped(r) or is_soft_clipped(m)):
        if (not r['is_reverse'] and m['is_reverse']) or (r['is_reverse'] and not m['is_reverse']):
            return (abs(r['ins_len']) < max_ins and abs(r['ins_len']) > min_ins) and \
                   (r['ref_end'] < (pos + params.tr)) and \
                   (m['ref_start'] > (pos - params.tr))
    return False

def is_supporting_split_read(r,pos,max_ins,sc_len):
    '''
    Return whether read is a supporting split read.
    Doesn't yet check whether the soft-clip aligns
    to the other side.
    '''
    if r['align_start'] < (params.tr): #a "soft" threshold if it is soft-clipped at the other end        
        return r['ref_end'] > (pos - params.tr) and r['ref_end'] < (pos + params.tr) and \
            (r['len'] - r['align_end'] >= sc_len) and abs(r['ins_len']) < max_ins
    else:
        return r['ref_start'] > (pos - params.tr) and r['ref_start'] < (pos + params.tr) and \
            (r['align_start'] >= sc_len) and abs(r['ins_len']) < max_ins

def is_supporting_split_read_wdir(bp_dir,r,pos,max_ins,sc_len):
    if bp_dir=='+':
        return r['ref_end'] > (pos - params.tr) and r['ref_end'] < (pos + params.tr) and \
            (r['len'] - r['align_end'] >= sc_len) and abs(r['ins_len']) < max_ins
    elif bp_dir=='-':
        return r['ref_start'] > (pos - params.tr) and r['ref_start'] < (pos + params.tr) and \
            (r['align_start'] >= sc_len) and abs(r['ins_len']) < max_ins
    else:
        return False

def is_supporting_split_read_lenient(r,pos):
    '''
    Same as is_supporting_split_read without insert and soft-clip threshold checks
    '''
    if r['align_start'] < (params.tr): #a "soft" threshold if it is soft-clipped at the other end        
        return (r['len'] - r['align_end'] >= params.tr) and r['ref_end'] > (pos - params.tr) and r['ref_end'] < (pos + params.tr)
    else:
        return (r['align_start'] >= params.tr) and r['ref_start'] > (pos - params.tr) and r['ref_start'] < (pos + params.tr)

def get_sc_bases(r,pos):
    '''
    Return the number of soft-clipped bases
    '''
    if r['align_start'] < (params.tr):
        return r['len'] - r['align_end']
    else:
        return r['align_start']

def get_bp_dist(x,bp_pos):
    if x['is_reverse']: 
        return (x['ref_end'] - bp_pos)
    else: 
        return (bp_pos - x['ref_start'])

def points_towards_break(x,pos):
    if x['is_reverse']:
        if x['ref_start'] + params.tr < pos: return False
    else: 
        if x['ref_end'] - params.tr > pos: return False
    return True

def is_supporting_spanning_pair(r,m,bp1,bp2,inserts,max_ins):
    pos1 = (bp1['start'] + bp1['end']) / 2
    pos2 = (bp2['start'] + bp2['end']) / 2
    
#    if is_soft_clipped(r) or is_soft_clipped(m):
#        return False
    
    #ensure this isn't just a regular old spanning pair    
    if r['chrom']==m['chrom']:
        if r['ref_start']<m['ref_start']:
            if m['ref_start']-r['ref_end'] < max_ins: return False
        else:
            if r['ref_start']-m['ref_end'] < max_ins: return False
    
    #check read orientation
    #spanning reads should always point towards the break
    if not points_towards_break(r,pos1) or not points_towards_break(m,pos2):
        return False

    ins_dist1 = get_bp_dist(r,pos1)
    ins_dist2 = get_bp_dist(m,pos2)

    if is_supporting_split_read_lenient(r,pos1):
        if is_soft_clipped(m): 
            #only allow one soft-clip
            return False
        if abs(ins_dist1)+abs(ins_dist2) < max_ins: 
            return True
    elif is_supporting_split_read_lenient(m,pos2):
        if is_soft_clipped(r):
            return False
        if abs(ins_dist1)+abs(ins_dist2) < max_ins:
            return True
    else:
        if ins_dist1>=-params.tr and ins_dist2>=-params.tr and abs(ins_dist1)+abs(ins_dist2) < max_ins:
            return True    

    return False

def get_loc_reads(bp,bamf,max_dp):
    loc = '%s:%d:%d' % (bp['chrom'], max(0,bp['start']), bp['end'])
    loc_reads = np.empty([0,len(params.read_dtype)],dtype=params.read_dtype)    
    try:
        iter_loc = bamf.fetch(region=loc,until_eof=True)
        for x in iter_loc:
            read = read_to_array(x,bamf) 
            if len(np.atleast_1d(read))>0:
                loc_reads = np.append(loc_reads,read)
            if len(loc_reads) > max_dp:
                print('Read depth too high at %s' % loc)
                return np.empty(0)
        loc_reads = np.sort(loc_reads,axis=0,order=['query_name','ref_start'])
        loc_reads = np.unique(loc_reads) #remove duplicates
        return loc_reads
    except ValueError:
        print('Fetching reads failed for loc: %s' % loc)
        return np.empty(0)

def reads_to_sam(reads,bam,bp1,bp2,name):
    '''
    For testing read assignemnts.
    Takes reads from array, matches them to bam 
    file reads by query name and outputs them to Sam
    '''
    bamf = pysam.AlignmentFile(bam, "rb")
    loc1 = '%s:%d:%d' % (bp1['chrom'], bp1['start'], bp1['end'])
    loc2 = '%s:%d:%d' % (bp2['chrom'], bp2['start'], bp2['end'])
    iter_loc1 = bamf.fetch(region=loc1,until_eof=True)
    iter_loc2 = bamf.fetch(region=loc2,until_eof=True)
    
    loc1 = '%s-%d' % (bp1['chrom'], (bp1['start']+bp1['end'])/2)
    loc2 = '%s-%d' % (bp2['chrom'], (bp1['start']+bp1['end'])/2)
    sam_name = '%s_%s-%s' % (name,loc1,loc2)
    bam_out = pysam.AlignmentFile('%s.sam'%sam_name, "w", header=bamf.header)
        
    for x in iter_loc1:
        if len(reads)==0:
            break
        if x.query_name in reads:
            bam_out.write(x)
            bam_out.write(bamf.mate(x))
            idx = int(np.where(reads==x.query_name)[0])
            reads = np.delete(reads,idx)

    for x in iter_loc2:
        if len(reads)==0:
            break
        if x.query_name in reads:
            bam_out.write(x)
            bam_out.write(bamf.mate(x))
            idx = int(np.where(reads==x.query_name)[0])
            reads = np.delete(reads,idx)

    bamf.close()
    bam_out.close()

def windowed_norm_read_count(loc_reads,inserts,min_ins,max_ins):
    '''
    Counts normal non-soft-clipped reads within window range
    '''
    cnorm = 0
    for idx,r in enumerate(loc_reads):
        if idx+1 >= len(loc_reads):
            break    
        r1 = np.array(loc_reads[idx],copy=True)
        r2 = np.array(loc_reads[idx+1],copy=True)
        if r1['query_name']!=r2['query_name'] or r1['chrom']!=r2['chrom']:
            continue
        ins_dist = r2['ref_end']-r1['ref_start']
        facing = not r1['is_reverse'] and r2['is_reverse']
        if not is_soft_clipped(r1) and not is_soft_clipped(r2) and facing and ins_dist > min_ins and ins_dist < max_ins:
            cnorm = cnorm + 2
    return cnorm

def get_loc_counts(bp,loc_reads,pos,rc,reproc,split,norm,min_ins,max_ins,sc_len,bp_num=1):
    for idx,x in enumerate(loc_reads):
        if idx+1 >= len(loc_reads):            
            break        
        r1 = loc_reads[idx]
        r2 = loc_reads[idx+1] if (idx+2)<=len(loc_reads) else None
        if is_normal_across_break(x,pos,min_ins,max_ins):
            norm = np.append(norm,r1)            
            split_norm = 'bp%d_split_norm'%bp_num
            norm_olap = 'bp%d_norm_olap_bp'%bp_num
            rc[split_norm] = rc[split_norm]+1 
            rc[norm_olap] = rc[norm_olap]+get_normal_overlap_bases(x,pos)
        elif is_supporting_split_read(x,pos,max_ins,sc_len):
            split = np.append(split,x)            
            split_supp = 'bp%d_split'%bp_num
            split_cnt = 'bp%d_sc_bases'%bp_num
            if bp['dir']!='?':
                if is_supporting_split_read_wdir(bp['dir'],x,pos,max_ins,sc_len):
                    rc[split_supp] = rc[split_supp]+1 
                    rc[split_cnt]  = rc[split_cnt]+get_sc_bases(x,pos)                    
            else:    
                rc[split_supp] = rc[split_supp]+1 
                rc[split_cnt]  = rc[split_cnt]+get_sc_bases(x,pos)
        elif r2!=None and r1['query_name']==r2['query_name'] and is_normal_spanning(r1,r2,pos,min_ins,max_ins):
            norm = np.append(norm,r1)            
            norm = np.append(norm,r2)            
            span_norm = 'bp%d_span_norm'%bp_num
            rc[span_norm] = rc[span_norm]+1 
        else:
            reproc = np.append(reproc,x) #may be spanning support or anomalous
    return rc, reproc, split, norm

def has_mixed_evidence(split,loc_reads,pos,sc_len):
    if len(split)>0:
        pos, total = sum(split['align_start'] < sc_len), len(split)
        if pos/float(total) > 0.2 and pos/float(total) < 0.8:
            return True
    else:
        split_reads = np.where([is_supporting_split_read_lenient(x,pos) for x in loc_reads])[0]
        split_all = loc_reads[split_reads]
        
        if len(split_all)>0:
            pos, total = sum(split_all['align_start'] < sc_len), len(split_all)
            if pos/float(total) > 0.2 and pos/float(total) < 0.8:
                return True

    return False

def get_dir_split(split,sc_len):
    align_mean =  np.mean(split['align_start'])
    assign_dir = '+' if align_mean < sc_len else '-'    
    return assign_dir

def get_dir_span(span):
    is_rev = np.sum(span['is_reverse'])
    assign_dir = '+' if is_rev <= len(span)/2 else '-'
    return assign_dir

def get_dir(split,loc_reads,pos,sc_len):    
    # split read direction tends to be more reliable
    if len(split)>0:
        dir_split = get_dir_split(split,sc_len)
        return dir_split        
    else:
        split_reads = np.where([is_supporting_split_read_lenient(x,pos) for x in loc_reads])[0]
        split_all = loc_reads[split_reads]
        if len(split_all)>0:
            dir_split = get_dir_split(split_all,sc_len)
            return dir_split
        else:
            return '?'
            #if len(span)>0:
            #    dir_span = get_dir_span(span)
            #    return dir_span 
            #else:

def bp_dir_matches_read_orientation(bp,pos,read):
    if bp['dir']=='+':
        return read['ref_start'] < pos and not read['is_reverse']
    elif bp['dir']=='-':
        return read['ref_end'] > pos and read['is_reverse']

def validate_spanning_orientation(bp1,bp2,r1,r2):
    pos1 = (bp1['start'] + bp1['end']) / 2
    pos2 = (bp2['start'] + bp2['end']) / 2
    
    r1_correct = bp_dir_matches_read_orientation(bp1,pos1,r1)
    r2_correct = bp_dir_matches_read_orientation(bp2,pos2,r2)
    
    return r1_correct and r2_correct

def get_spanning_counts(reproc,rc,bp1,bp2,inserts,min_ins,max_ins):
    pos1 = (bp1['start'] + bp1['end']) / 2
    pos2 = (bp2['start'] + bp2['end']) / 2
    
    reproc = np.sort(reproc,axis=0,order=['query_name','ref_start'])
    reproc = np.unique(reproc) #remove dups
    reproc_again = np.empty([0,len(params.read_dtype)],dtype=params.read_dtype)    
    span_bp1 = np.empty([0,len(params.read_dtype)],dtype=params.read_dtype)
    span_bp2 = np.empty([0,len(params.read_dtype)],dtype=params.read_dtype)
    
    for idx,x in enumerate(reproc):
        if idx+1 >= len(reproc):
            break        
        if reproc[idx+1]['query_name'] != reproc[idx]['query_name']:
            #not paired
            continue

        mate = np.array(reproc[idx+1],copy=True)
        r1 = np.array(x,copy=True)
        r2 = np.array(mate,copy=True)

        #if read corresponds to bp2 and mate to bp1
        if (bp1['chrom']!=bp2['chrom'] and x['chrom']==bp2['chrom']) or \
            (pos1 > pos2 and bp1['chrom']==bp2['chrom']):
            r1 = mate
            r2 = np.array(x,copy=True)
        if is_supporting_spanning_pair(r1,r2,bp1,bp2,inserts,max_ins):        
            span_bp1 = np.append(span_bp1,r1)
            span_bp2 = np.append(span_bp2,r2)
            if bp1['dir']!='?' and bp2['dir']!='?':
                if validate_spanning_orientation(bp1,bp2,r1,r2):
                    rc['spanning'] = rc['spanning']+1
                else:
                    reproc_again = np.append(reproc,x)
            else:
                rc['spanning'] = rc['spanning']+1
        else:
            reproc_again = np.append(reproc,x)
    return rc,span_bp1,span_bp2,reproc_again

def recount_split_reads(split_reads,pos,bp_dir,max_ins,sc_len):
    split_count = 0
    split_bases = 0
    for idx,x in enumerate(split_reads):
        if is_supporting_split_read_wdir(bp_dir,x,pos,max_ins,sc_len):
            split_count += 1
            split_bases += get_sc_bases(x,pos)
    return split_count,split_bases        

def get_sv_read_counts(bp1,bp2,bam,inserts,max_dp,min_ins,max_ins,sc_len):
    bamf = pysam.AlignmentFile(bam, "rb")
    pos1 = (bp1['start'] + bp1['end']) / 2
    pos2 = (bp2['start'] + bp2['end']) / 2
    loc1_reads = get_loc_reads(bp1,bamf,max_dp)    
    loc2_reads = get_loc_reads(bp2,bamf,max_dp)    
    bamf.close()
    
    if len(loc1_reads)==0 or len(loc2_reads)==0:
        return np.empty(0,dtype=params.sv_out_dtype)
    
    rc = np.zeros(1,dtype=params.sv_out_dtype)
    reproc = np.empty([0,len(params.read_dtype)],dtype=params.read_dtype)    
    rc['bp1_total_reads'] = len(loc1_reads)
    rc['bp2_total_reads'] = len(loc2_reads)

    split_bp1 = np.empty([0,len(params.read_dtype)],dtype=params.read_dtype)
    split_bp2 = np.empty([0,len(params.read_dtype)],dtype=params.read_dtype)
    norm = np.empty([0,len(params.read_dtype)],dtype=params.read_dtype)
    
    rc, reproc, split_bp1, norm = get_loc_counts(bp1,loc1_reads,pos1,rc,reproc,split_bp1,norm,min_ins,max_ins,sc_len)
    rc, reproc, split_bp2, norm = get_loc_counts(bp2,loc2_reads,pos2,rc,reproc,split_bp2,norm,min_ins,max_ins,sc_len,2)
    rc['bp1_win_norm'] = windowed_norm_read_count(loc1_reads,inserts,min_ins,max_ins)
    rc['bp2_win_norm'] = windowed_norm_read_count(loc2_reads,inserts,min_ins,max_ins)    

    #TODO: cleanup/refactor
    if bp1['dir']!='-' or bp1['dir']!='+':
        if has_mixed_evidence(split_bp1,loc1_reads,pos1,sc_len):
            rc['bp1_dir'] = '?'
            rc['classification'] = 'REPROC'
        else:
            rc['bp1_dir'] = get_dir(split_bp1,loc1_reads,pos1,sc_len)
            if rc['bp1_dir'] == '?':
                rc['classification'] = 'UNKNOWN_DIR'
            else:
                rc['bp1_split'], rc['bp1_sc_bases'] = recount_split_reads(split_bp1,pos1,rc[0]['bp1_dir'],max_ins,sc_len)
    else:
        rc['bp1_dir'] = bp1['dir']

    if bp2['dir']!='-' or bp2['dir']!='+':
        if has_mixed_evidence(split_bp2,loc2_reads,pos2,sc_len):
            rc['bp2_dir'] = '?'
            rc['classification'] = 'REPROC'
        else:    
            if rc['bp2_dir'] == '?':
                rc['classification'] = 'UNKNOWN_DIR'
            else:
                rc['bp2_dir'] = get_dir(split_bp2,loc2_reads,pos1,sc_len)            
                rc['bp2_split'], rc['bp2_sc_bases'] = recount_split_reads(split_bp2,pos2,rc[0]['bp2_dir'],max_ins,sc_len)
    else:
        rc['bp2_dir'] = bp2['dir']
    
    rc, span_bp1, span_bp2, reproc = get_spanning_counts(reproc,rc,bp1,bp2,inserts,min_ins,max_ins)
    
    # for debugging only
    #span_reads = np.unique(np.concatenate([span_bp1['query_name'],span_bp2['query_name']]))
    #reads_to_sam(span_reads,bam,bp1,bp2,'span')

    print('processed %d reads at loc1; %d reads at loc2' % (len(loc1_reads),len(loc2_reads)))
    return rc

def get_params(bam,mean_dp,max_cn,rlen,insert_mean,insert_std,out):
    inserts = [insert_mean,insert_std]
    if rlen<0:
        #rlen = bamtools.estimateTagSize(bam)
        rlen = 101
    if inserts[0]<0 or inserts[1]<0:
        inserts = bamtools.estimateInsertSizeDistribution(bam)
    else:
        inserts[0] = inserts[0]+(rlen*2) #derive fragment size
    
    max_dp = ((mean_dp*(params.window*2))/rlen)*max_cn
    max_ins = inserts[0]+(2*inserts[1]) #actually the max *fragment* size
    #min_ins = max(rlen*2,inserts[0]-(2*inserts[1])) #actually the min *fragment* size
    min_ins = rlen*2
    
    with open('%s_params.txt'%out,'w') as outp:
        outp.write('read_len\tinsert_mean\tinsert_std\tinsert_min\tinsert_max\tmax_dep\n')
        outp.write('%d\t%f\t%f\t%f\t%f\t%d'%(rlen,inserts[0]-(rlen*2),inserts[1],min_ins-(rlen*2),max_ins-(rlen*2),max_dp))

    return rlen, inserts, max_dp, max_ins, min_ins

def remove_duplicates(svs):
    for idx,row in enumerate(svs):
        #reorder breakpoints based on position or chromosomes
        bp1_chr, bp1_pos, bp1_dir, bp2_chr, bp2_pos, bp2_dir, sv_class = row
        if (bp1_chr!=bp2_chr and bp1_chr>bp2_chr) or (bp1_chr==bp2_chr and bp1_pos > bp2_pos):
            svs[idx] = (bp2_chr,bp2_pos,bp2_dir,bp1_chr,bp1_pos,bp1_dir,sv_class)
    return np.unique(svs)

def load_input_vcf(svin,class_field):
    sv_dtype = [s for i,s in enumerate(params.sv_dtype) if i not in [2,5]]
    
    sv_vcf = vcf.Reader(filename=svin)
    sv_dict = OrderedDict()
    for sv in sv_vcf:
        
        if sv.FILTER is not None:
            if len(sv.FILTER)>0:
                continue
        
        sv_dict[sv.ID] = {'CHROM': sv.CHROM, 'POS': sv.POS, 'INFO': sv.INFO}

#    svs = OrderedDict()
#    sv_vcf = np.genfromtxt(svin,dtype=params.sv_vcf_dtype,delimiter='\t',comments="#")
#    keys = [key[0] for key in params.sv_vcf_dtype]
#    
#    for sv in sv_vcf:
#        sv_id = sv['ID']
#        svs[sv_id] = OrderedDict()
#        for key,sv_data in zip(keys,sv):
#            if key=='INFO' or key=='ID': continue
#            svs[sv_id][key] = sv_data
#         
#        info = map(methodcaller('split','='),sv['INFO'].split(';'))
#        svs[sv_id]['INFO'] = OrderedDict()
#        
#        for i in info:
#            if len(i)<2: continue
#            name = i[0]
#            data = i[1]
#            svs[sv_id]['INFO'][name] = data
    
    svs = np.empty(0,sv_dtype)
    procd = np.empty(0,dtype='S50')

    for sv_id in sv_dict:
        try:
            sv = sv_dict[sv_id]
            mate_id = sv['INFO']['MATEID']
            mate = sv_dict[mate_id]
            
            if (sv_id in procd) or (mate_id in procd): 
                continue
            
            bp1_chr = sv['CHROM']
            bp1_pos = sv['POS']
            bp2_chr = mate['CHROM']
            bp2_pos = mate['POS']
            sv_class = sv['INFO'][class_field] if class_field!='' else ''

            procd = np.append(procd,[sv_id,mate_id])
            new_sv = np.array([(bp1_chr,bp1_pos,bp2_chr,bp2_pos,sv_class)],dtype=sv_dtype)        
            svs = np.append(svs,new_sv)
        except KeyError:
            print("SV %s improperly paired or missing attributes"%sv_id)
            continue
    
    return svs

def load_input_socrates(svin,rlen,use_dir,min_mapq,filt_repeats):
    #sv_dtype =  [s for s in params.sv_dtype] if use_dir else [s for i,s in enumerate(params.sv_dtype) if i not in [2,5]]
    sv_dtype = params.sv_dtype
    
    #TODO: make parsing of socrates input more robust
    soc_in = np.genfromtxt(svin,delimiter='\t',names=True,dtype=None,invalid_raise=False)
    svs = np.empty(0,dtype=sv_dtype)
    filtered_out = 0

    for row in soc_in:
        bp1 = row[params.bp1_pos].split(':')
        bp2 = row[params.bp2_pos].split(':')
        bp1_chr, bp1_pos = bp1[0], int(bp1[1]) 
        bp2_chr, bp2_pos = bp2[0], int(bp2[1])
        #classification = row['classification']
        if row[params.avg_mapq1]<min_mapq or row[params.avg_mapq2]<min_mapq:
            filtered_out += 1
            continue
        if filt_repeats!=[]:
            try:
                if row[params.repeat1] in filt_repeats and row[params.repeat2] in filt_repeats:
                    filtered_out += 1
                    continue
            except IndexError:
                raise Exception('''Supplied Socrates file does not contain index %s, 
                please check the repeat field name matches the parameters.py file''')
        add_sv = np.empty(0)
        
        bp1_dir = row[params.bp1_dir] if use_dir else '?'
        bp2_dir = row[params.bp1_dir] if use_dir else '?'
        
        add_sv = np.array([(bp1_chr,bp1_pos,bp1_dir,bp2_chr,bp2_pos,bp2_dir,'')],dtype=sv_dtype)
        svs = np.append(svs,add_sv)
    
    print('Filtered out %d Socrates SVs, keeping %d SVs' % (filtered_out,len(svs)))            
    return remove_duplicates(svs)

def load_input_simple(svin,use_dir,class_field):
    #sv_dtype =  [s for s in params.sv_dtype] if use_dir else [s for i,s in enumerate(params.sv_dtype) if i not in [2,5]]
    sv_dtype = params.sv_dtype

    sv_tmp = np.genfromtxt(svin,delimiter='\t',names=True,dtype=None,invalid_raise=False)
    svs = np.empty(0,dtype=sv_dtype)
    for row in sv_tmp:
        bp1_chr = str(row['bp1_chr'])
        bp1_pos = int(row['bp1_pos'])
        bp2_chr = str(row['bp2_chr'])
        bp2_pos = int(row['bp2_pos'])
        sv_class = row[class_field] if class_field!='' else ''
        add_sv = np.empty(0)
        bp1_dir = str(row['bp1_dir']) if use_dir else '?'
        bp2_dir = str(row['bp2_dir']) if use_dir else '?'
        add_sv = np.array([(bp1_chr,bp1_pos,bp1_dir,bp2_chr,bp2_pos,bp2_dir,sv_class)],dtype=sv_dtype)
        svs = np.append(svs,add_sv)
    return remove_duplicates(svs)

def reprocess_unknown_sv_types(sv_info,bamf):
    
    bp_dtype = [('chrom','S20'),('start', int), ('end', int), ('dir', 'S1')]
    bp1_chr, bp1_pos, bp1_dir, bp2_chr, bp2_pos, bp2_dir, sv_class = [h[0] for h in params.sv_dtype]
    reproc = sv_info[sv_info['classification']=='REPROC']

    #unfinished
    ipdb.set_trace()

def classify_event(sv,sv_id,svd_prevResult,prevSV):    
    
    svd_result = svd.detect(prevSV,svd_prevResult,sv)
    svd_prevResult,prevSV = svd_result,sv

    classification = svd.getResultType(svd_result)
    sv_id = sv_id if svd_result[0]==svd.SVtypes.interspersedDuplication else sv_id+1
    
    return classification, sv_id, svd_prevResult, prevSV

def proc_svs(args):
    
    svin         = args.svin
    bam          = args.bam
    out          = args.out
    mean_dp      = float(args.mean_dp)
    sc_len       = int(args.sc_len)
    max_cn       = int(args.max_cn)
    rlen         = int(args.rlen)
    insert_mean  = float(args.insert_mean)
    insert_std   = float(args.insert_std)
    simple       = args.simple_svs
    socrates     = args.socrates
    use_dir      = args.use_dir
    filt_repeats = args.filt_repeats
    min_mapq     = args.min_mapq
    class_field  = args.class_field

    filt_repeats = filt_repeats.split(',') if filt_repeats != '' else filt_repeats
    filt_repeats = [rep for rep in filt_repeats if rep!='']    
   
    if not (simple or socrates): use_dir = False #vcfs don't have dirs

    outf = '%s_svinfo.txt'%out
    
    dirname = os.path.dirname(out)
    if dirname!='' and not os.path.exists(dirname):
        os.makedirs(dirname)

    rlen, inserts, max_dp, max_ins, min_ins = get_params(bam, mean_dp, max_cn, rlen, insert_mean, insert_std, out)

    # write header output
    header_out = ['ID'] + [h[0] for idx,h in enumerate(params.sv_dtype) if idx not in [2,5,6]] #don't include dir fields
    header_out.extend([h[0] for h in params.sv_out_dtype])
    
    with open('%s_svinfo.txt'%out,'w') as outf:        
        writer = csv.writer(outf,delimiter='\t',quoting=csv.QUOTE_NONE)
        writer.writerow(header_out)

    bp_dtype = [('chrom','S20'),('start', int), ('end', int), ('dir', 'S1')]
    bp1_chr, bp1_pos, bp1_dir, bp2_chr, bp2_pos, bp2_dir, sv_class = [h[0] for h in params.sv_dtype]
    
    svs = np.empty(0)
    if simple:
        svs = load_input_simple(svin,use_dir,class_field)
    elif socrates:
        svs = load_input_socrates(svin,rlen,use_dir,min_mapq,filt_repeats)
    else:
        svs = load_input_vcf(svin,class_field)

    print("Extracting data from %d SVs"%len(svs))
    svd_prevResult, prevSV = None, None
    sv_id = 0
    for row in svs:        
        sv_prop = row[bp1_chr],row[bp1_pos],row[bp2_chr],row[bp2_pos]
        sv_str = '%s:%d|%s:%d'%sv_prop
        print('processing %s'%sv_str)

        sv_rc = np.empty(0)
        bp1 = np.array((row[bp1_chr],row[bp1_pos]-params.window,row[bp1_pos]+params.window,row[bp1_dir]),dtype=bp_dtype)
        bp2 = np.array((row[bp2_chr],row[bp2_pos]-params.window,row[bp2_pos]+params.window,row[bp2_dir]),dtype=bp_dtype)
        sv_rc  = get_sv_read_counts(bp1,bp2,bam,inserts,max_dp,min_ins,max_ins,sc_len)

        if len(sv_rc) > 0:
            for idx, svi in enumerate(sv_rc):
                norm1 = int(svi['bp1_split_norm']+svi['bp1_span_norm'])
                norm2 = int(svi['bp2_split_norm']+svi['bp2_span_norm'])
                support = float(svi['bp1_split'] + svi['bp2_split'] + svi['spanning'])
            
                sv_rc[idx]['norm1'] = norm1 
                sv_rc[idx]['norm2'] = norm2
                sv_rc[idx]['support'] = support
                sv_rc[idx]['vaf1'] = support / (support + norm1) if support!=0 else 0
                sv_rc[idx]['vaf2'] = support / (support + norm2) if support!=0 else 0            
              
                if sv_rc['classification']=='':
                    sv = (row[bp1_chr],row[bp1_pos],svi[bp1_dir],row[bp2_chr],row[bp2_pos],svi[bp2_dir])
                    sv_rc[idx]['classification'], sv_id, svd_prevResult, prevSV = classify_event(sv,sv_id,svd_prevResult,prevSV)
                elif class_field!='' and sv_rc['bp1_dir']!='?' and sv_rc['bp2_dir']!='?':
                    sv_id = sv_id+1
                    sv_rc[idx]['classification'] = row[sv_class]
                else:
                    sv_id += 1
        else:
            sv_id = sv_id+1
            sv_rc = np.zeros(1,dtype=params.sv_out_dtype)
            sv_rc['classification'] = 'HIDEP'

        sv_out = [sv_id] + [r for idx,r in enumerate(row) if idx not in [2,5,6]]
        sv_out.extend([rc for rc in sv_rc[0]])

        with open('%s_svinfo.txt'%out,'a') as outf:
            writer = csv.writer(outf,delimiter='\t',quoting=csv.QUOTE_NONE)
            writer.writerow(sv_out)

#    TODO: finish reprocessing code
#    if not use_dir:
#        rewrite=False
#        sv_info = np.genfromtxt('%s_svinfo.txt'%out,delimiter='\t',names=True,dtype=None)
#        if 'REPROC' in sv_info['classification']:
#            rewrite = True
#            sv_info = reprocess_unknown_sv_types(sv_info,bam)
#        if rewrite:
#            with open('%s_svinfo.txt'%out,'w') as outf:
#                writer = csv.writer(outf,delimiter='\t',quoting=csv.QUOTE_NONE)
#                writer.writerow(header_out)
#                for sv_out in sv_info:
#                    writer.writerow(sv_out)

    #post-process: look for translocations
    if class_field=='':
        rewrite = False
        sv_info = np.genfromtxt('%s_svinfo.txt'%out,delimiter='\t',names=True,dtype=None)
        trx_label    = svd.getResultType([svd.SVtypes.translocation])
        intins_label = svd.getResultType([svd.SVtypes.interspersedDuplication])
        for idx,sv in enumerate(sv_info):
            if sv['classification']==intins_label:
                rewrite=True
                sv_info[idx-1]['classification'] = intins_label
                translocs = svd.detectTransloc(idx,sv_info)
                if len(translocs)>0:
                    for i in translocs: sv_info[i]['classification'] = trx_label
        if rewrite:
            with open('%s_svinfo.txt'%out,'w') as outf:
                writer = csv.writer(outf,delimiter='\t',quoting=csv.QUOTE_NONE)
                writer.writerow(header_out)
                for sv_out in sv_info:
                    writer.writerow(sv_out)
