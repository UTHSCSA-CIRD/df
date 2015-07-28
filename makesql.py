""" Generate dynamic data extraction SQL for DataBuilder output files
---------------------------------------------------------------------
    
 Usage:
   makesql sqltemplate.sql dbname.db
"""

import sqlite3 as sq,argparse,re,csv,time,ConfigParser,pdb
cfg = ConfigParser.RawConfigParser()
cfg.read('sqldump.cfg')
par=dict(cfg.items("Settings"))

parser = argparse.ArgumentParser()
parser.add_argument("dbfile",help="SQLite file generated by DataBuilder")
parser.add_argument("-c","--cleanup",help="Restore dbfile to its vanilla, data-builder state",action="store_true")
parser.add_argument("-v","--csvfile",help="File to write output to, in addition to the tables that will get created in the dbfile. By default this is whatever was the name of the dbfile with '.csv' substituted for '.db'",default='OUTFILE')
parser.add_argument("-s","--style",help="What style to output the file, currently there are two-- concat which concatenates the code variables and simple which represents code variables as Yes/No, with the nulls represented by No. The default is concat.",default="concat",choices=['concat','simple'])
parser.add_argument("-d","--datecompress",help="Round all dates to the nearest X days, default is 1",default=1)
args = parser.parse_args()

# location of data dictionary sql file
ddsql = "sql/dd.sql"
# TODO: make these passable via command-line argument for customizability
binvals = ['No','Yes']
# this says how many joins to permit per sub-table
joffset = 60

from datafinisher_fn import *


def main(cnx,fname,style,dtcp):
    tt = time.time(); startt = tt
    # create a cursor, though most of the time turns out we don't need it because the connection
    # also has an execute() method.
    #cur = cnx.cursor()
    # declare some custom functions to use within SQL queries (awesome!)
    cnx.create_function("grs",2,ifgrp)
    cnx.create_function("shw",2,shortenwords)
    cnx.create_function("drl",1,dropletters)
    cnx.create_aggregate("dgr",2,diaggregate)
    cnx.create_aggregate("igr",11,infoaggregate)
    cnx.create_aggregate("xgr",11,debugaggregate)
    # not quite foolproof-- still pulls in PROCID's, but in the final version we'll be filtering on this
    icd9grep = '.*\\\\([VE0-9]{3}(\\.[0-9]{0,2}){0,1})\\\\.*'
    loincgrep = '\\\\([0-9]{4,5}-[0-9])\\\\COMPONENT'

    # DONE (ticket #1): instead of relying on sqlite_denorm.sql, create the scaffold table from inside this 
    # script by putting the appropriate SQL commands into character strings and then passing those
    # strings as arguments to execute() (see below for an example of cur.execute() usage (cur just happens 
    # to be what we named the cursor object we created above, and execute() is a method that cursor objects have)
    # DONE: create an id to concept_cd mapping table (and filtering out redundant facts taken care of here)
    # TODO: parameterize the fact-filtering
    # create a log table
    cnx.execute("""create table if not exists dflog as
      select datetime() timestamp,
      'FirstEntryKey                                     ' key,
      'FirstEntryVal                                     ' val""")
    
    # certain values should not be changed after the first run
    cnx.execute("CREATE TABLE if not exists dfvars ( varname TEXT, textval TEXT, numval NUM )")
    olddtcp = cnx.execute("select numval from dfvars where varname = 'dtcp'").fetchall()
    if len(olddtcp) == 0:
      cnx.execute("insert into dfvars (varname,numval) values ('dtcp',"+str(dtcp)+")")
      cnx.commit()
      print "First run since cleanup, apparently"
    elif len(olddtcp) == 1:
      if dtcp != olddtcp:
	dtcp = olddtcp[0][0]
	print "Warning! Ignoring requested datecompress value and using previously stored value of "+str(dtcp)
	print "To get rid of it, do `python makesql.py -c dbfile`"
    else:
      print "Uh oh. Something is wrong there should not be more than one 'dtcp' entry in dfvars, debug time"
        
    if cnx.execute("select count(*) from modifier_dimension").fetchone()[0] == 0:
      print "modifier_dimension is empty, let's fill it"
      # we load our local fallback db
      cnx.execute("attach './sql/datafinisher.db' as dfdb")
      # and copy from it into the input .db file's modifier_dimension
      cnx.execute("insert into modifier_dimension select * from dfdb.modifier_dimension")
      # and log that we did so
      cnx.execute("insert into dflog select datetime(),'insert','modifier_dimension'")
      cnx.commit()

    tprint("initialized variables",tt);tt = time.time()

    # cur.execute("drop table if exists scaffold")
    # turns out it was not necessary to create an empty table first for scaffold-- the date problem 
    # that this was supposed to solve was being caused by something else, so here is the more concise
    # version that may also be a little faster
    cnx.execute(par['create_scaffold'].format(rdst(dtcp)))
    cnx.execute("CREATE UNIQUE INDEX if not exists df_ix_scaffold ON scaffold (patient_num,start_date) ")
    tprint("created scaffold table and index",tt);tt = time.time()

    # cnx.execute("drop table if exists cdid")
    cnx.execute(par['cdid_tab'])
    tprint("created cdid table",tt);tt = time.time()

    # diagnoses
    cnx.execute("""update cdid set cpath = grs('"""+icd9grep+"""',cpath) where ddomain like '%|DX_ID' """)
    cnx.execute("""update cdid set cpath = substr(ccd,instr(ccd,':')+1) where ddomain = 'ICD9'""")
    # LOINC
    cnx.execute("""update cdid set cpath = grs('"""+loincgrep+"""',cpath) where ddomain like '%|COMPONENT_ID' """)
    cnx.execute("""update cdid set cpath = substr(ccd,instr(ccd,':')+1) where ddomain = 'LOINC'""")
    cnx.execute("create UNIQUE INDEX if not exists df_ix_cdid ON cdid (id,cpath,ccd)")
    cnx.commit()
    tprint("mapped concept codes in cdid",tt);tt = time.time()
    
    # The obs_df table may make most of the views unneccessary
    cnx.execute(par['obs_df'].format(rdst(dtcp)))
    cnx.execute("create INDEX if not exists df_ix_obs ON obs_df(pn,sd,concept_cd,instance_num,modifier_cd)")
    cnx.commit()
    tprint("created obs_df table and index",tt);tt = time.time()
    
    # create the ruledefs table
    # the current implementation is just a temporary hack so that the rest of the script will run
    # TODO: As per Ticket #19, this needs to be changed so the rules get read in from sql/ruledefs.csv
    create_ruledef(cnx, par['ruledefs'])
        
    tprint("created rule definitions",tt);tt = time.time()
    #cur.execute("drop table if exists data_dictionary")
    with open(ddsql,'r') as ddf:
	ddcreate = ddf.read()
    cnx.execute(ddcreate)
    # rather than running the same complicated select statement multiple times for each rule in data_dictionary
    # lets just run each selection criterion once and save it as a tag in the new RULE column
    tprint("created data_dictionary",tt);tt = time.time()

    # diagnosis
    cnx.execute(par['dd_diag'])
    # LOINC
    cnx.execute(par['dd_loinc'])
    # code-only
    cnx.execute(par['dd_vvital'])
    # visit vitals
    cnx.execute(par['dd_code_only'])
    # code-and-mod only
    cnx.execute(par['dd_codemod_only'])
    # of the concepts in this column, only one is recorded at a time
    cnx.commit()
    tprint("added rules to data_dictionary",tt);tt = time.time()
    
    # create the dd2 table, which may make most of these individually defined tables unnecessary
    cnx.execute(par['dd2'])
    tprint("created dd2 table",tt);tt = time.time()
    
    # each row in dd2 will correspond to one column in the output
    # here we break dd2 into more manageable chunks
    numjoins = cnx.execute("select count(distinct jcode) from dd2").fetchone()[0]
    [cnx.execute(par['chunkdd2'].format(ii,joffset)) for ii in range(0,numjoins,joffset)]
    cnx.commit();
    tprint("assigned chunks to dd2",tt);tt = time.time()
    
    # code for creating all the temporary tables
    import pdb; pdb.set_trace()
    [cnx.execute(ii[0]) for ii in cnx.execute(par['maketables']).fetchall()]
    tprint("created all tables described by dd2",tt);tt = time.time()
    
    # code for creating what will eventually replace the fulloutput table
    cnx.execute(cnx.execute(par['fulloutput2']).fetchone()[0])
    tprint("created fulloutput2 table",tt);tt = time.time()
    
    allsel = rdt('birth_date',dtcp)+""" birth_date, sex_cd 
      ,language_cd, race_cd, julianday(scaffold.start_date) - julianday("""+rdt('birth_date',dtcp)+") age_at_visit_days,"""
    dd2sel = cnx.execute("select group_concat(colname) from dd2").fetchone()[0]
    
    allqry = "create table if not exists fulloutput as select scaffold.*," + allsel + dd2sel
    allqry += """ from scaffold 
      left join patient_dimension pd on pd.patient_num = scaffold.patient_num
      left join fulloutput2 fo on fo.patient_num = scaffold.patient_num and fo.start_date = scaffold.start_date
      """
    allqry += " order by patient_num, start_date"
    import pdb;pdb.set_trace()
    cnx.execute(allqry)
    tprint("created fulloutput table",tt);tt = time.time()

    binoutqry = """create view binoutput as select patient_num,start_date,birth_date,sex_cd
		   ,language_cd,race_cd,age_at_visit_days,"""
    binoutqry += dd2sel
    #binoutqry += ","+",".join([ii[1] for ii in cnx.execute("pragma table_info(loincfacts)").fetchall()[2:]])
    binoutqry += " from fulloutput"
    cnx.execute("drop view if exists binoutput")
    cnx.execute(binoutqry)
    tprint("created binoutput view",tt);tt = time.time()

    if style == 'simple':
      finalview = 'binoutput'
    else:
      finalview = 'fulloutput'
      
    if fname.lower() != 'none':
      ff = open(fname,'wb')
      csv.writer(ff).writerow([ii[1] for ii in con.execute("PRAGMA table_info(fulloutput)").fetchall()])
      result = cnx.execute("select * from "+finalview).fetchall()
      with ff:
	  csv.writer(ff).writerows(result)
    tprint("wrote output table to file",tt);tt = time.time()
    tprint("TOTAL RUNTIME",startt)

        
    """
    TODO: implement a user-configurable 'rulebook' containing patterns for catching data that would otherwise fall 
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
      
    #import pdb; pdb.set_trace();
    #import code; code.interact(local=vars())
    if args.cleanup:
      cleanup(con)
    else:
      main(con,csvfile,args.style,dtcp)



