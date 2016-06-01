""" Generate dynamic data extraction SQL for DataBuilder output files
---------------------------------------------------------------------
usage: df.py [-h] [-l] [-c] [-v CSVFILE] [-s {concat,simple}] [-d DATECOMPRESS] dbfile
    
"""

import sqlite3 as sq,argparse,re,csv,time,ConfigParser,pdb
from os.path import dirname
cwd = dirname(__file__)
if cwd == '': cwd = '.'
cfg = ConfigParser.RawConfigParser()
cfg.read(cwd+'/sql/df.cfg')
par=dict(cfg.items("Settings"))

parser = argparse.ArgumentParser()
parser.add_argument("-l","--log",help="Log verbose sql",action="store_true")
parser.add_argument("dbfile",help="SQLite file generated by DataBuilder")
parser.add_argument("-c","--cleanup",help="Restore dbfile to its vanilla, data-builder state",action="store_true")
parser.add_argument("-v","--csvfile",help="File to write output to, in addition to the tables that will get created in the dbfile. By default this is whatever was the name of the dbfile with '.csv' substituted for '.db'",default='OUTFILE')
parser.add_argument("-s","--style",help="What style to output the file, currently there are two-- concat which concatenates the code variables and simple which represents code variables as Yes/No, with the nulls represented by No. The default is concat.",default="concat",choices=['concat','simple'])
parser.add_argument("-d","--datecompress",help="Round all dates to the nearest X days, default is 1",default=1)
args = parser.parse_args()

# location of data dictionary sql file
ddsql = cwd + "/sql/dd.sql"
# TODO: make these passable via command-line argument for customizability
binvals = ['No','Yes']
# this says how many joins to permit per sub-table
joffset = 60
dolog = args.log

from df_fn import *


def main(cnx,fname,style,dtcp):
    tt = time.time(); startt = tt
    # declare some custom functions to use within SQL queries (awesome!)
    # returns matching regexp if found, otherwise original string
    cnx.create_function("grs",2,ifgrp)
    # regexp replace... i.e. sed
    cnx.create_function("grsub",3,subgrp)
    # omit "least relevant" words to make a character string shorter
    cnx.create_function("shw",2,shortenwords)
    # shorten words by squeezing out certain characters
    cnx.create_function("drl",1,dropletters)
    # pythonish string formatting with replacement
    cnx.create_function("pyf",5,pyformat)
    cnx.create_function("pyf",4,pyformat)
    cnx.create_function("pyf",3,pyformat)
    cnx.create_function("pyf",2,pyformat)
    # trim and concatenate arguments
    cnx.create_function("tc",4,trimcat)
    cnx.create_function("tc",3,trimcat)
    cnx.create_function("tc",2,trimcat)
    # string aggregation specific for diagnoses and codes that behave like them
    cnx.create_aggregate("dgr",2,diaggregate)
    # string aggregation for user-specified fields
    cnx.create_aggregate("igr",11,infoaggregate)
    # the kitchen-sink aggregator that tokenizes and concatenates everything
    cnx.create_aggregate("xgr",11,debugaggregate)
    cnx.create_aggregate("sqgr",6,sqlaggregate)
    
    # TODO: this is a hardcoded dependency on LOINC and ICD9 strings in paths! 
    #       This is not an i2b2-ism, it's an EPICism, and possibly a HERONism
    #       Should be configurable!
    # regexps to use with grs SQL UDF (above)
    # not quite foolproof-- still pulls in PROCID's, so we filter for DX_ID
    # for ICD9 codes embedded in paths
    icd9grep = '.*\\\\([VE0-9]{3}(\\.[0-9]{0,2}){0,1})\\\\.*'
    # for ICD9 codes embedded in i2b2 CONCEPT_CD style codes
    icd9grep_c = '^ICD9:([VE0-9]{3}(\\.[0-9]{0,2}){0,1})$'
    # for LOINC codes embedded in paths
    loincgrep = '\\\\([0-9]{4,5}-[0-9])\\\\COMPONENT'
    # for LOINC codes embedded in i2b2 CONCEPT_CD style codes
    loincgrep_c = '^LOINC:([0-9]{4,5}-[0-9])$'
    

    # DONE (ticket #1): instead of relying on sqlite_denorm.sql, create the df_joinme table from inside this 
    # script by putting the appropriate SQL commands into character strings and then passing those
    # strings as arguments to execute() (see below for an example of cur.execute() usage (cur just happens 
    # to be what we named the cursor object we created above, and execute() is a method that cursor objects have)
    # DONE: create an id to concept_cd mapping table (and filtering out redundant facts taken care of here)
    # TODO: parameterize the fact-filtering

    # Variable persistence not fully implemented and this implementation might 
    # not be a good idea. If this block (through the "Uh oh...") isn't broken, 
    # ignore it for now. Ditto with datafinisher_log, but used even less.
    # create a log table
    logged_execute(cnx, """create table if not exists datafinisher_log as
      select datetime() timestamp,
      'FirstEntryKey                                     ' key,
      'FirstEntryVal                                     ' val""")
    # certain values should not be changed after the first run
    logged_execute(cnx, "CREATE TABLE if not exists df_vars ( varname TEXT, textval TEXT, numval NUM )")
    # TODO: oldtcp is a candidate for renaming
    olddtcp = logged_execute(cnx, "select numval from df_vars where varname = 'dtcp'").fetchall()
    if len(olddtcp) == 0:
      logged_execute(cnx, "insert into df_vars (varname,numval) values ('dtcp',"+str(dtcp)+")")
      cnx.commit()
      print "First run since cleanup, apparently"
    elif len(olddtcp) == 1:
      if dtcp != olddtcp:
	dtcp = olddtcp[0][0]
	print "Warning! Ignoring requested datecompress value and using previously stored value of "+str(dtcp)
	print "To get rid of it, do `python df.py -c dbfile`"
    else:
      print "Uh oh. Something is wrong there should not be more than one 'dtcp' entry in df_vars, debug time"

    # Sooner or later we will need to write rules that make modifier codes human readable
    # E.g.: allergies, family history. MODIFIER_DIMENSION has mappings for such codes. If 
    # the site providing the databuilder file did not include any entries in its MODIFIER_DIMENSION
    # we use our own, below.
    if logged_execute(cnx, "select count(*) from modifier_dimension").fetchone()[0] == 0:
      print "modifier_dimension is empty, let's fill it"
      # we load our local fallback db
      logged_execute(cnx, "attach '{0}/sql/datafinisher.db' as dfdb".format(cwd))
      # and copy from it into the input .db file's modifier_dimension
      logged_execute(cnx, "insert into modifier_dimension select * from dfdb.modifier_dimension")
      # and log that we did so
      logged_execute(cnx, "insert into datafinisher_log select datetime(),'insert','modifier_dimension'")
      cnx.commit()

    # tprint is what echoes progress to console
    tprint("initialized variables",tt);tt = time.time()
    # Vivek was here!    
    pdb.set_trace()
    # df_joinme has all unique patient_num and start_date combos, and therefore it defines
    # which rows will exist in the output CSV file. All other columns that get created
    # will be joined to it
    logged_execute(cnx, par['create_joinme'].format(rdst(dtcp)))
    logged_execute(cnx, "CREATE UNIQUE INDEX if not exists df_ix_df_joinme ON df_joinme (patient_num,start_date) ")
    tprint("created df_joinme table and index",tt);tt = time.time()

    # the CDID table maps concept codes (CCD) to variable id (ID) to 
    # data domain (DDOMAIN) to concept path (CPATH)
    logged_execute(cnx, par['create_codeid_tmp'])
    tprint("created df_codeid_tmp table",tt);tt = time.time()
    
    # Now we will replace the EHR-specific concept paths simply with the most 
    # granular available standard concept code (so far only for ICD9 and LOINC)
    # TODO: more generic compression of terminal code-nodes (RXNorm, CPT, etc.)

    # diagnoses
    logged_execute(cnx, "update df_codeid_tmp set cpath = grs('"+icd9grep+"',cpath) where ddomain like '%|DX_ID'")
    # TODO: the below might be more performant in current SQLite versions, might want to put it
    # back in after adding a version check
    # logged_execute(cnx, """update df_codeid set cpath = substr(ccd,instr(ccd,':')+1) where ddomain = 'ICD9'""")
    logged_execute(cnx, "update df_codeid_tmp set cpath = replace(ccd,'ICD9:','') where ddomain = 'ICD9'")
    # LOINC
    logged_execute(cnx, "update df_codeid_tmp set cpath = grs('"+loincgrep+"',cpath) where ddomain like '%|COMPONENT_ID'")
    # LOINC nodes modified analogously to ICD9 nodes above
    #logged_execute(cnx, """update df_codeid set cpath = substr(ccd,instr(ccd,':')+1) where ddomain = 'LOINC'""")
    logged_execute(cnx, "update df_codeid_tmp set cpath = replace(ccd,'LOINC:','') where ddomain = 'LOINC'")
    # df_codeid gets created here from the distinct values of df_codeid_tmp
    logged_execute(cnx, par['create_codeid'])
    logged_execute(cnx, "create UNIQUE INDEX if not exists df_ix_df_codeid ON df_codeid (id,cpath,ccd)")
    cnx.commit()
    logged_execute(cnx, "drop table if exists df_codeid_tmp")
    tprint("mapped concept codes in df_codeid",tt);tt = time.time()
    
    # The create_obsfact table may make most of the views unneccessary... it did!
    logged_execute(cnx, par['create_obsfact'].format(rdst(dtcp)))
    logged_execute(cnx, "create INDEX if not exists df_ix_obs ON df_obsfact(pn,sd,concept_cd,instance_num,modifier_cd)")
    cnx.commit()
    tprint("created df_obsfact table and index",tt);tt = time.time()
    
    
    # DONE: As per Ticket #19, this was changed so the rules get read 
    # in from ./ruledefs.csv and a df_rules table is created from it
    #create_ruledef(cnx, '{0}/{1}'.format(cwd, par['ruledefs']))
    #
    # we make the subsection() function declared in df_fn.py a 
    # method of ConfigParser
    ConfigParser.ConfigParser.subsection = subsection
    cnf = ConfigParser.ConfigParser()
    cnf.read('sql/test.cfg')
    ruledicts = [cnf.subsection(ii) for ii in cnf.sections()]
    # replacement for df_rules
    if len(logged_execute(cnx,"pragma table_info('df_rules')").fetchall()) < 1:
      logged_execute(cnx,"""CREATE TABLE df_rules 
		     (sub_slct_std UNKNOWN_TYPE_STRING, sub_payload UNKNOWN_TYPE_STRING
		     , sub_frm_std UNKNOWN_TYPE_STRING, sbwr UNKNOWN_TYPE_STRING
		     , sub_grp_std UNKNOWN_TYPE_STRING, presuffix UNKNOWN_TYPE_STRING
		     , suffix UNKNOWN_TYPE_STRING, concode UNKNOWN_TYPE_BOOLEAN NOT NULL
		     , rule UNKNOWN_TYPE_STRING NOT NULL, grouping INTEGER NOT NULL
		     , subgrouping INTEGER NOT NULL, in_use UNKNOWN_TYPE_BOOLEAN NOT NULL
		     , criterion UNKNOWN_TYPE_STRING)""");
      #logged_execute(cnx,"delete from df_rules"); cnx.commit();
      # we read our cnf.subsection()s in...
      # populate the df_rules table to make sure result matches the .csv rules
      [cnx.execute("insert into df_rules ({0}) values (\" {1} \")".format(
	",".join(ii.keys()),' "," '.join(ii.values()))) for ii in ruledicts if ii['in_use']=='1']
    tprint("created rule definitions",tt);tt = time.time()

    # Read in and run the sql/dd.sql file
    with open(ddsql,'r') as ddf:
	ddcreate = ddf.read()
    logged_execute(cnx, ddcreate)
    tprint("created df_dtdict",tt);tt = time.time()

    # rather than running the same complicated select statement multiple times 
    # for each rule in df_dtdict lets just run each selection criterion 
    # once and save it as a tag in the new RULE column
    # DONE: use df_rules
    # This is a possible place to use the new dsSel function (see below)
    #[logged_execute(cnx, ii[0]) for ii in logged_execute(cnx, par['dd_criteria']).fetchall()]
    #cnx.commit()
    dd_criteria = [dsSel(ii['rule'],ii['criterion'],"""
			 update df_dtdict set rule = '{0}'
			 where rule = 'UNKNOWN_DATA_ELEMENT' and 
			 """) for ii in ruledicts if ii['rule']!='UNKNOWN_DATA_ELEMENT']
    [logged_execute(cnx,ii) for ii in set(dd_criteria)]
    cnx.commit()
    tprint("added rules to df_dtdict",tt);tt = time.time()
    
    # create the create_dynsql table, which may make most of these individually defined tables unnecessary
    # see if the ugly code hiding behind par['create_dynsql'] can be replaced by 
    # more concise dsSel Or maybe even if df_dynsql table itself can be replaced 
    # and we could do it all in one step
    # DONE: use df_rules
    logged_execute(cnx, par['create_dynsql'])
    tprint("created df_dynsql table",tt);tt = time.time()
    
    # not sure it's an improvement, but here is using the sqgr function nested in itself 
    # to create the equivalent of the df_dynsql table 
    #(note the kludgy replace and || stuff, needs to be done better)
    # the body of the query
    foo = cnx.execute("select sqgr(lv,rv,lf,' ',rf,' ') from (select sub_slct_std||sqgr(trim(colcd)||trim(presuffix)||trim(suffix),'',replace(sub_payload,'ccode',0),'','','')||replace(sub_frm_std,'{cid}','''{0}''')||sbwr lf,colcd lv,replace(sub_grp_std,'jcode',0) rf,trim(colcd)||trim(presuffix) rv from df_rules join df_dtdict on trim(df_rules.rule) = trim(df_dtdict.rule) where concode=0 group by cid order by cid,grouping,subgrouping)").fetchall()
    # or maybe even
    foo1 = " ".join([ii[0] for ii in cnx.execute("select pyf(sub_slct_std||sqgr(tc(colcd,presuffix,suffix),'',replace(sub_payload,'ccode',0),'','','')||replace(sub_frm_std,'{cid}','''{0}''')||sbwr||replace(sub_grp_std,'jcode',1) ,colcd,tc(colcd,presuffix)) from df_rules join df_dtdict on trim(df_rules.rule) = trim(df_dtdict.rule) where concode=0 group by cid order by cid,grouping,subgrouping").fetchall()])
    # doesn't currently work, but will when we replace the {} stuff permanently
    """
    foo2 = " ".join([ii[0] for ii in cnx.execute("select pyf(sub_slct_std||sqgr(tc(colcd,presuffix,suffix),'',sub_payload,'','','')||sub_frm_std||sbwr||sub_grp_std,colcd,tc(colcd,presuffix)) from df_rules join df_dtdict on trim(df_rules.rule) = trim(df_dtdict.rule) where concode=0 group by cid order by cid,grouping,subgrouping").fetchall()])
    """
    # the select part of the query
    bar = cnx.execute("select group_concat(val) from (select distinct trim(colcd)||trim(presuffix)||trim(suffix) val from df_rules join df_dtdict on trim(df_rules.rule) = trim(df_dtdict.rule) where concode=0 order by cid,grouping,subgrouping)").fetchall()
    # or maybe even
    bar1=cnx.execute("select group_concat(val) from (select distinct tc(colcd,presuffix,suffix) val from df_rules join df_dtdict on trim(df_rules.rule) = trim(df_dtdict.rule) where concode=0 order by cid,grouping,subgrouping)").fetchall()[0][0]
    # putting them together...
    "select patient_num,start_date, "+bar[0][0]+" from df_joinme "+foo[0][0]

    
    # each row in create_dynsql will correspond to one column in the output
    # here we break create_dynsql into more manageable chunks
    # again, if generated using dsSel, we might be able to manage those chunks script-side
    numjoins = logged_execute(cnx, "select count(distinct jcode) from df_dynsql").fetchone()[0]
    [logged_execute(cnx, par['chunk_dynsql'].format(ii,joffset)) for ii in range(0,numjoins,joffset)]
    cnx.commit();
    tprint("assigned chunks to df_dynsql",tt);tt = time.time()
    
    # code for creating all the temporary tables
    # where cmh.db slows down
    [logged_execute(cnx, ii[0]) for ii in logged_execute(cnx, par['maketables']).fetchall()]
    tprint("created all tables described by df_dynsql",tt);tt = time.time()
    
    # code for creating what will eventually replace the fulloutput table
    logged_execute(cnx, logged_execute(cnx, par['fulloutput2']).fetchone()[0])
    tprint("created fulloutput2 table",tt);tt = time.time()
    
    # TODO: lots of variables being created here, therefore candidates for renaming
    # or refactoring to make simpler
    allsel = rdt('birth_date',dtcp)+""" birth_date, sex_cd 
      ,language_cd, race_cd, julianday(df_joinme.start_date) - julianday("""+rdt('birth_date',dtcp)+") age_at_visit_days,"""
    dynsqlsel = logged_execute(cnx, "select group_concat(colname) from df_dynsql").fetchone()[0]
    
    allqry = "create table if not exists fulloutput as select df_joinme.*," + allsel + dynsqlsel
    allqry += """ from df_joinme 
      left join patient_dimension pd on pd.patient_num = df_joinme.patient_num
      left join fulloutput2 fo on fo.patient_num = df_joinme.patient_num and fo.start_date = df_joinme.start_date
      """
    allqry += " order by patient_num, start_date"
    logged_execute(cnx, allqry)
    tprint("created fulloutput table",tt);tt = time.time()

    selbin_dynsql = logged_execute(cnx, par['selbin_dynsql']).fetchone()[0]
    binoutqry = """create view df_binoutput as select patient_num,start_date,birth_date,sex_cd
		   ,language_cd,race_cd,age_at_visit_days,"""
    binoutqry += selbin_dynsql
    #binoutqry += ","+",".join([ii[1] for ii in logged_execute(cnx, "pragma table_info(loincfacts)").fetchall()[2:]])
    binoutqry += " from fulloutput"
    logged_execute(cnx, "drop view if exists df_binoutput")
    logged_execute(cnx, binoutqry)
    tprint("created df_binoutput view",tt);tt = time.time()

    if style == 'simple':
      finalview = 'df_binoutput'
    else:
      finalview = 'fulloutput'
      
    # i.e. to not create a .csv file, pass 'none' in the -v argument
    if fname.lower() != 'none':
      ff = open(fname,'wb')
      # below line generates the CSV header row
      csv.writer(ff).writerow([ii[1] for ii in con.execute("PRAGMA table_info("+finalview+")").fetchall()])
      result = logged_execute(cnx, "select * from "+finalview).fetchall()
      with ff:
	  csv.writer(ff).writerows(result)
	  
    tprint("wrote output table to file",tt);tt = time.time()
    tprint("TOTAL RUNTIME",startt)
    
    pdb.set_trace()
    """
    DONE: implement a user-configurable 'rulebook' containing patterns for catching data that would otherwise fall 
    into UNKNOWN FALLBACK, and expressing in a parseable form what to do when each rule is triggered.
    DONE: The data dictionary will contain information about which built-in or user-configured rule applies for each cid
    We are probably looking at several different 'dcat' style tables, broken up by type of data
    DONE: We will iterate through the data dictionary, joining new columns to the result according to the applicable rule
    """
    
    
	
if __name__ == '__main__':
    con = sq.connect(args.dbfile)

    if args.csvfile == 'OUTFILE':
      csvfile = args.dbfile.replace(".db","")+".csv"
    else:
      csvfile = args.csvfile

    if args.datecompress == 'week':
      dtcp = 7
    elif args.datecompress == 'month':
      dtcp = 365.25/12
    else:
      dtcp = args.datecompress
      
    if args.cleanup:
      cleanup(con)
    else:
      main(con,csvfile,args.style,dtcp)



