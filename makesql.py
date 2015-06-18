""" Generate dynamic data extraction SQL for DataBuilder output files
---------------------------------------------------------------------
    
 Usage:
   makesql sqltemplate.sql dbname.db
"""

import sqlite3 as sq,argparse,re,csv,time

parser = argparse.ArgumentParser()
parser.add_argument("dbfile",help="SQLite file generated by DataBuilder")
parser.add_argument("-c","--cleanup",help="Restore dbfile to its vanilla, data-builder state",action="store_true")
parser.add_argument("-v","--csvfile",help="File to write output to, in addition to the tables that will get created in the dbfile. By default this is whatever was the name of the dbfile with '.csv' substituted for '.db'",default='OUTFILE')
parser.add_argument("-s","--style",help="What style to output the file, currently there are two-- concat which concatenates the code variables and simple which represents code variables as Yes/No, with the nulls represented by No. The default is concat.",default="concat",choices=['concat','simple'])
parser.add_argument("-d","--datecompress",help="Round all dates to the nearest X days, default is 1",default=1)
args = parser.parse_args()

# location of data dictionary sql file
ddsql = "sql/dd.sql"

# this is to register a SQLite function for pulling out matching substrings (if found)
# and otherwise returning the original string. Useful for extracting ICD9, CPT, and LOINC codes
# from concept paths where they are embedded. For ICD9 the magic pattern is:
# '.*\\\\([VE0-9]{3}\.{0,1}[0-9]{0,2})\\\\.*'
def ifgrp(pattern,txt):
    rs = re.search(re.compile(pattern),txt)
    
    if rs == None:
      return txt 
    else:
      return rs.group(1)

def cleanup(cnx):
    t_drop = ['cdid','codefacts','codemodfacts','diagfacts','loincfacts','data_dictionary',
	      'fulloutput','oneperdayfacts','scaffold','unkfacts','unktemp','dfvars']
    v_drop = ['obs_all','obs_diag_active','obs_diag_inactive','obs_labs','obs_noins']
    print "Dropping views"
    for ii in v_drop:
      cnx.execute("drop view if exists "+ii)
    print "Dropping tables"
    for ii in t_drop:
      cnx.execute("drop table if exists "+ii)
      
# The rdt and rdst functions aren't exactly user-defined SQLite functions...
# They are python function that emit a string to concatenate into a larger SQL query
# and send back to SQL... because SQLite has a native julianday() function that's super
# easy to use. So, think of rdt and rdst as pseudo-UDFs
def rdt(datecol,factor):
    if factor == 1:
      return 'date('+datecol+')'
    else:
      return 'date(round(julianday('+datecol+')/'+str(factor)+')*'+str(factor)+')'
    
def rdst(factor):
    return rdt('start_date',factor)

def shortenwords(words,limit):
	""" Initialize the data, lengths, and indexes"""
	#get rid of the numeric codes
	words = re.sub('[0-9]','',words)
	wrds = words.split(); lens = map(len,wrds); idxs=range(len(lens))
	if limit >= len(words):
	  return(words)
	""" sort the indexes and lengths"""
	idxs.sort(key=lambda xx: lens[xx]); lens.sort()
	""" initialize the threshold and the vector of 'most important' words"""
	sumidx=0; keep=[]
	# turned out that checking the lengths of the lens and idxs is what it takes to avoid crashes
	while sumidx < limit and len(lens) > 0 and len(idxs) > 0:
		sumidx += lens.pop()
		keep.append(idxs.pop())
	keep.sort()
	shortened = [wrds[ii] for ii in keep]
	return " ".join(shortened)

def dropletters(intext):
	# This function shortens words by squeezing out vowels, most non-alphas, and repeating letters
	# the first regexp replaces multiple ocurrences of the same letter with one ocurrence of that letter
	# the \B matches a word boundary... so we only replace vowels from inside words, not leading lettters
	return re.sub(r"([a-z_ ])\1",r"\1",re.sub("\B[aeiouyAEIOUY]+","",re.sub("[^a-zA-Z _]"," ", intext)))


def main(cnx,fname,style,dtcp):
    start_time = time.time()
    # create a cursor, though most of the time turns out we don't need it because the connection
    # also has an execute() method.
    cur = cnx.cursor()
    # declare some custom functions to use within SQL queries (awesome!)
    cnx.create_function("grs",2,ifgrp)
    cnx.create_function("shw",2,shortenwords)
    cnx.create_function("drl",1,dropletters)
    # not quite foolproof-- still pulls in PROCID's, but in the final version we'll be filtering on this
    icd9grep = '.*\\\\([VE0-9]{3}(\\.[0-9]{0,2}){0,1})\\\\.*'
    loincgrep = '\\\\([0-9]{4,5}-[0-9])\\\\COMPONENT'
    
    # todo: make these passable via command-line argument for customizability
    binvals = ['No','Yes']
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
      import pdb; pdb.set_trace()    
        
    if cnx.execute("select count(*) from modifier_dimension").fetchone()[0] == 0:
      print "modifier_dimension is empty, let's fill it"
      # we load our local fallback db
      cnx.execute("attach './sql/datafinisher.db' as dfdb")
      # and copy from it into the input .db file's modifier_dimension
      cnx.execute("insert into modifier_dimension select * from dfdb.modifier_dimension")
      # and log that we did so
      cnx.execute("insert into dflog select datetime(),'insert','modifier_dimension'")
      cnx.commit()
    
    print "Creating scaffold table"
    # cur.execute("drop table if exists scaffold")
    # turns out it was not necessary to create an empty table first for scaffold-- the date problem 
    # that this was supposed to solve was being caused by something else, so here is the more concise
    # version that may also be a little faster
    cnx.execute("""create table if not exists scaffold as
    select distinct patient_num, """+rdst(dtcp)+""" start_date
    from observation_fact order by patient_num, start_date;
    """)

    cnx.execute("CREATE UNIQUE INDEX if not exists df_ix_scaffold ON scaffold (patient_num,start_date) ")

    print "Creating CDID table"
    # cnx.execute("drop table if exists cdid")
    cnx.execute("""
	create table cdid as
	select distinct concept_cd ccd,id
	,substr(concept_cd,1,instr(concept_cd,':')-1) ddomain
	,cd.concept_path cpath
	from concept_dimension cd 
	join (select min(id) id,min(concept_path) concept_path 
	from variable 
	where name not like '%old at visit' and name not in ('Living','Deceased','Not recorded','Female','Male','Unknown')
	group by item_key) vr
	on cd.concept_path like vr.concept_path||'%'
	""")
    print "Mapping concept codes in CDID"
    # diagnoses
    cnx.execute("""update cdid set cpath = grs('"""+icd9grep+"""',cpath) where ddomain like '%|DX_ID' """)
    cnx.execute("""update cdid set cpath = substr(ccd,instr(ccd,':')+1) where ddomain = 'ICD9'""")
    # LOINC
    cnx.execute("""update cdid set cpath = grs('"""+loincgrep+"""',cpath) where ddomain like '%|COMPONENT_ID' """)
    cnx.execute("""update cdid set cpath = substr(ccd,instr(ccd,':')+1) where ddomain = 'LOINC'""")
    cnx.execute("create UNIQUE INDEX if not exists df_ix_cdid ON cdid (id,cpath,ccd)")
    cnx.commit()
    # create a couple of cleaned-up views of observation_fact
    # replace most of the non-informative values with nulls, remove certain known redundant modifiers
    print "Creating obs_all and obs_noins views"
    cur.execute("drop view if exists obs_all")
    cur.execute("""
	create view obs_all as
	select distinct patient_num,concept_cd,"""+rdst(dtcp)+""" start_date,modifier_cd
	,case when valtype_cd in ('@','N','') then null else valtype_cd end valtype_cd
	,instance_num
	,case when tval_char in ('@','') then null else tval_char end tval_char
	,nval_num
	,case when valueflag_cd in ('@','') then null else valueflag_cd end valueflag_cd
	,quantity_num
	,units_cd,location_cd,confidence_num from observation_fact
	where modifier_cd not in ('Labs|Aggregate:Last','Labs|Aggregate:Median','PROCORDERS:Outpatient','DiagObs:PROBLEM_LIST')
	and concept_cd not like 'DEM|AGEATV:%' and concept_cd not like 'DEM|SEX:%' and concept_cd not like 'DEM|VITAL:%'
	""");
    cur.execute("drop view if exists obs_noins")
    # it would be better to aggregate multiple numeric values of the same fact collected on the same day by median, but alas
    # not all versions of SQLite have support for the median function
    cur.execute("""
	create view obs_noins as 
        select patient_num,concept_cd,start_date,modifier_cd,valtype_cd,tval_char,avg(nval_num) nval_num
        ,group_concat(distinct valueflag_cd) valueflag_cd,group_concat(distinct quantity_num) quantity_num
        ,units_cd,group_concat(distinct location_cd) location_cd
        ,group_concat(distinct confidence_num) confidence_num from (
	  select distinct patient_num,concept_cd,"""+rdst(dtcp)+""" start_date,modifier_cd
	  ,case when valtype_cd in ('@','') then null else valtype_cd end valtype_cd
	  ,case when tval_char in ('@','') then null else tval_char end tval_char
	  ,nval_num
	  ,case when valueflag_cd in ('@','') then null else valueflag_cd end valueflag_cd
	  ,quantity_num
	  ,units_cd,location_cd,confidence_num from observation_fact
	  where modifier_cd not in ('Labs|Aggregate:Last','Labs|Aggregate:Median','PROCORDERS:Outpatient','DiagObs:PROBLEM_LIST')
	  and concept_cd not like 'DEM|AGEATV:%' and concept_cd not like 'DEM|SEX:%' and concept_cd not like 'DEM|VITAL:%'
        ) group by patient_num,start_date,concept_cd,modifier_cd,units_cd""");
    
    print "Creating OBS_DIAG_ACTIVE view"
    cur.execute("drop view if exists obs_diag_active")
    cur.execute("""
      create view obs_diag_active as
      select distinct patient_num pn,"""+rdst(dtcp)+""" sd,id,cpath
      ,replace('{'||group_concat(distinct modifier_cd)||'}','DiagObs:','') modifier_cd
      from observation_fact join cdid on concept_cd = ccd 
      where modifier_cd not in ('DiagObs:MEDICAL_HX','PROBLEM_STATUS_C:2','PROBLEM_STATUS_C:3','DiagObs:PROBLEM_LIST')
      group by patient_num,"""+rdst(dtcp)+""",cpath,id
      """)
    print "Creating OBS_DIAG_INACTIVE view"
    cur.execute("drop view if exists obs_diag_inactive")
    cur.execute("""
      create view obs_diag_inactive as
      select distinct patient_num pn,"""+rdst(dtcp)+""" sd,id,cpath
      ,replace('{'||group_concat(distinct modifier_cd)||'}','DiagObs:','') modifier_cd
      from observation_fact join cdid on concept_cd = ccd 
      where modifier_cd in ('DiagObs:MEDICAL_HX','PROBLEM_STATUS_C:2','PROBLEM_STATUS_C:3')
      group by patient_num,"""+rdst(dtcp)+""",cpath,id
      """)
    print "Creating obs_labs view"
    cur.execute("drop view if exists obs_labs")
    cur.execute("""
      create view obs_labs as
      select distinct patient_num pn,"""+rdst(dtcp)+""" sd,id,cpath,avg(nval_num) nval_num
      ,group_concat(distinct units_cd) units
      ,case when coalesce(group_concat(distinct tval_char),'E')='E' then '' else group_concat(distinct tval_char) end ||
      case when coalesce(group_concat(distinct valueflag_cd),'@')='@' then '' else ' flag:'||
      group_concat(distinct valueflag_cd) end || case when count(*) > 1 then ' cnt:'||count(*) else '' end info
      from observation_fact join cdid on concept_cd = ccd
      where modifier_cd = '@' and ddomain = 'LOINC' or ddomain like '%COMPONENT_ID'
      group by patient_num,"""+rdst(dtcp)+""",cpath,id
      """)
    
    # DONE: instead of a with-clause temp-table create a static data dictionary table
    #		var(concept_path,concept_cd,ddomain,vid) 
    # BTW, turns out this is a way to read and execute a SQL script
    # TODO: the shortened column names will go into this data dictionary table
    # DONE: create a filtered static copy of OBSERVATION_FACT with a vid column, maybe others
    # no vid column, relationship between concept_cd and id is not 1:1, so could get too big
    # will instead cross-walk the cdid table as needed
    # ...but perhaps unnecessary now that cdid table exists
    
    print "Creating DATA_DICTIONARY"
    #cur.execute("drop table if exists data_dictionary")
    with open(ddsql,'r') as ddf:
	ddcreate = ddf.read()
    cur.execute(ddcreate)
    # rather than running the same complicated select statement multiple times for each rule in data_dictionary
    # lets just run each selection criterion once and save it as a tag in the new RULE column
    print "Creating rules in DATA_DICTIONARY"
    # diagnosis
    cur.execute("""
	update data_dictionary set rule = 'diag' where ddomain like '%ICD9%DX_ID%' or ddomain like '%DX_ID%ICD9%'
	and rule = 'UNKNOWN_DATA_ELEMENT'
	""")
    # LOINC
    cur.execute("""
	update data_dictionary set rule = 'loinc' where ddomain like '%LOINC%COMPONENT_ID%' 
	or ddomain like '%COMPONENT_ID%LOINC%'
	and rule = 'UNKNOWN_DATA_ELEMENT'
	""")
    # code-only
    cur.execute("""
        update data_dictionary set rule = 'code' where
        coalesce(mod,tval_char,valueflag_cd,units_cd,confidence_num,quantity_num,location_cd,valtype_cd,nval_num,-1) = -1
        and rule = 'UNKNOWN_DATA_ELEMENT'
        """)
    # code-and-mod only
    cur.execute("""
        update data_dictionary set rule = 'codemod' where
        coalesce(tval_char,valueflag_cd,units_cd,confidence_num,quantity_num,location_cd,valtype_cd,nval_num,-1) = -1
        and mod is not null and rule = 'UNKNOWN_DATA_ELEMENT'""")
    # of the concepts in this column, only one is recorded at a time
    cur.execute("update data_dictionary set rule = 'oneperday' where mxfacts = 1 and rule = 'UNKNOWN_DATA_ELEMENT'")
    cnx.commit()
    
    print "Creating dynamic SQL for CODEFACTS"
    cur.execute("select group_concat(colid) from data_dictionary where rule = 'code'")
    codesel = cur.fetchone()[0]
    # dynamically generate the terms in the select statement
    # extract the terms that meet the above criterion
    codeqry = "create table if not exists codefacts as select scaffold.*,"+codesel+" from scaffold "
    # now dynamically generate the many, many join clauses and append them to codefacts
    # note the string replace-- cannot alias the table name in an update statement, so no dd
    cur.execute("""
	select ' left join (select patient_num,start_date sd
	,replace(group_concat(distinct concept_cd),'','',''; '') '||colid||' from cdid 
	join obs_noins on ccd = concept_cd where id = '||cid||' group by patient_num
        ,start_date order by patient_num,start_date) '||colid||' 
        on '||colid||'.patient_num = scaffold.patient_num 
        and '||colid||'.sd = scaffold.start_date' from data_dictionary where rule = 'code'""")
    codeqry += " ".join([row[0] for row in cur.fetchall()])
    print "Creating CODEFACTS table"
    cur.execute(codeqry) 
    # same pattern as above, but now for facts that consist of both codes and modifiers

    print "Creating dynamic SQL for CODEMODFACTS"
    # select terms...
    cur.execute("select group_concat(colid) from data_dictionary where rule = 'codemod'")
    codemodsel = cur.fetchone()[0]
    codemodqry = "create table if not exists codemodfacts as select scaffold.*,"+codemodsel+" from scaffold "
    # ...and joins...
    cur.execute("""
        select ' left join (select patient_num,start_date sd
        ,replace(group_concat(distinct concept_cd||''=''||modifier_cd),'','',''; '') '||colid||' from cdid 
        join obs_noins on ccd = concept_cd where id = '||cid||' group by patient_num
        ,start_date order by patient_num,start_date) '||colid||' 
        on '||colid||'.patient_num = scaffold.patient_num 
        and '||colid||'.sd = scaffold.start_date' from data_dictionary where rule = 'codemod'""")
    codemodqry += " ".join([row[0] for row in cur.fetchall()])
    print "Creating CODEMODFACTS table"
    cur.execute(codemodqry)
    
    # DONE: cid's (column id's i.e. groups of variables that were selected together by the researcher)
    # ...cid's that have a ccd value of 1 (meaning there is only one distinct concept code per cid
    # any variable that doesn't have multiple values on the same day 
    # (except multiple instances of numeric values which get averaged)
    # these are expected to be numeric variables
    # TODO: create a column in obs_noins with a count of duplicates that got averaged, for QC
    print "Creating dynamic SQL for ONEPERDAY"
    # here are the select terms, but a little more complicated than in the above cases
    # on the fence whether to have extra column for the code
    # ','||colid||'_cd'||
    cur.execute("""select 
	(case when mod is null then '' else ','||colid||'_mod' end)||
	(case when tval_char is null then '' else ','||colid||'_txt' end )||
	(case when valueflag_cd is null then '' else ','||colid||'_flg' end )||
	(case when units_cd is null then '' else ','||colid||'_unt' end )||
	(case when confidence_num is null then '' else ','||colid||'_cnf' end )||
	(case when quantity_num is null then '' else ','||colid||'_qnt' end )||
	(case when location_cd is null then '' else ','||colid||'_loc' end )||
	(case when valtype_cd is null then '' else ','||colid||'_typ' end )||
	(case when nval_num is null then '' else ','||colid end)
	from data_dictionary where rule = 'oneperday'""")
    oneperdaysel = " ".join([row[0] for row in cur.fetchall()])
    oneperdayqry = "create table if not exists oneperdayfacts as select scaffold.*" + oneperdaysel + " from scaffold "
    # since we're doing ALL the non-aggregate columns at the same time, the above query is designed
    # to produce multiple rows, so we change the earlier pattern slightly so we can glue them all together
    # joins
    cur.execute("""
	select 'left join (select patient_num,start_date'||
	(case when mod is null then '' else ',modifier_cd '||colid||'_mod ' end)||
	(case when tval_char is null then '' else ',tval_char '||colid||'_txt ' end )||
	(case when valueflag_cd is null then '' else ',valueflag_cd '||colid||'_flg ' end )||
	(case when units_cd is null then '' else ',units_cd '||colid||'_unt ' end )||
	(case when confidence_num is null then '' else ',confidence_num '||colid||'_cnf ' end )||
	(case when quantity_num is null then '' else ',quantity_num '||colid||'_qnt ' end )||
	(case when location_cd is null then '' else ',location_cd '||colid||'_loc ' end )||
	(case when valtype_cd is null then '' else ',valtype_cd '||colid||'_typ ' end )||
	(case when nval_num is null then '' else ',nval_num '||colid end)||
	' from obs_noins join cdid on ccd = concept_cd where id = '||cid||') '||colid||
	' on '||colid||'.start_date = scaffold.start_date and '||
	colid||'.patient_num = scaffold.patient_num'
	from data_dictionary where rule = 'oneperday'""")
    oneperdayqry += " ".join([row[0] for row in cur.fetchall()])
    print "Creating ONEPERDAYFACTS table"
    cur.execute(oneperdayqry)
    # diagnoses output tables

    print "Creating dynamic SQL for DIAG"
    cur.execute("""
      select group_concat(colid||','||colid||'_inactive') from data_dictionary where rule = 'diag'
      """)
    diagsel = cur.fetchone()[0]
    diagqry = "create table if not exists diagfacts as select scaffold.*,"+diagsel+" from scaffold "
    cur.execute("""
      select 'left join (select pn,sd,replace(group_concat(distinct cpath||''=''||modifier_cd),'','','';'') '||colid||' from obs_diag_active '||colid||' where id='||cid||' group by pn,sd) '||colid||' on '||colid||'.pn = scaffold.patient_num and '||colid||'.sd = scaffold.start_date' from data_dictionary where rule ='diag'
      union all
      select 'left join (select pn,sd,replace(group_concat(distinct cpath||''=''||modifier_cd),'','','';'') '||colid||'_inactive from obs_diag_inactive '||colid||'_inactive where id='||cid||' group by pn,sd) '||colid||'_inactive on '||colid||'_inactive.pn = scaffold.patient_num and '||colid||'_inactive.sd = scaffold.start_date' from data_dictionary where rule ='diag' 
      """)
    diagqry += " ".join([row[0] for row in cur.fetchall()])
    print "Creating DIAGFACTS table"
    cur.execute(diagqry)
    
    # DONE: create the LOINCFACTS table which will contain: pn,sd,nval_num,units,info,and cpath as part of the colid
    print "Creating dynamic SQL for LOINC"
    loincsel = cnx.execute("""
      select replace(group_concat(distinct colid||'_'||cpath||
      '_value,'||colid||'_'||cpath||'_units,'||colid||'_'||cpath||'_info'),'-','_')
      from obs_labs join data_dictionary on cid = id
      where rule = 'loinc' order by cid""").fetchone()[0]
    loincqry = "create table if not exists loincfacts as select scaffold.*,"+loincsel+" from scaffold "
    # okay, so the below is insane and should probably be refactored
    # We have the usual " ".join(blah blah blah) to create the join clauses
    # But the query that creates those clauses replaces all hyphens with underscores so that the
    # dynamically generated column names will be legal ones... but in one place in each subquery, 
    # there really should be a hyphen instead of an uderscore: where the cpath is matched to a 
    # LOINC code. So, on the python side, we change those and only those underscores back to hyphens
    # I know, pretty f*ck*d up, isn't it?
    loincqry += re.compile("cpath=(['][[0-9]{4,5})_").sub(r'cpath=\1-'," ".join([row[0] for row in cnx.execute("""
      select distinct
	replace('left join (select distinct pn,sd,cpath,nval_num '||colid||'_'||cpath||'_value'||
	',units '||colid||'_'||cpath||'_units'||
	',info '||colid||'_'||cpath||'_info'||
	' from obs_labs where id='||cid||
	' and cpath='''||cpath||''') '||colid||'_'||cpath||
	' on '||colid||'_'||cpath||'.pn = scaffold.patient_num and '||
	colid||'_'||cpath||'.sd = scaffold.start_date','-','_')
	from obs_labs join data_dictionary on cid = id
	where rule = 'loinc' order by cid""").fetchall()]))
    cnx.execute(loincqry)
   
    # DONE: fallback on giant messy concatenated strings for everything else (for now)
    print "Creating dynamic SQL for UNKTEMP and UNKFACTS tables"
    cur.execute("""select group_concat(colid),
	group_concat('left join (select patient_num pn,start_date sd,megacode '||colid||
	    ' from unktemp where id = '||cid||') '||colid||' on '||colid||'.pn = patient_num 
	      and '||colid||'.sd = start_date ',' '),
	group_concat(cid) from data_dictionary where rule = 'UNKNOWN_DATA_ELEMENT'""")
    unkqryvars = cur.fetchone()
    unkqry0 = """create table if not exists unktemp as 
	select patient_num,"""+rdst(dtcp)+""" start_date,id
	,group_concat(distinct concept_cd||coalesce('&mod='||modifier_cd,'')||
	coalesce('&ins='||instance_num,'')||coalesce('&typ='||valtype_cd,'')||
	coalesce('&txt='||tval_char,'')||coalesce('&num='||nval_num,'')||
	coalesce('&flg='||valueflag_cd,'')||coalesce('&qty='||quantity_num,'')||
	coalesce('&unt='||units_cd,'')||coalesce('&loc='||location_cd,'')||
	coalesce('&cnf='||confidence_num,'')) megacode
	from obs_all join cdid on concept_cd = ccd
	where id in ("""+unkqryvars[2]+") group by patient_num,start_date,id"
    unkqry1 = "create table if not exists unkfacts as select scaffold.*,"+unkqryvars[0]+" from scaffold "
    unkqry1 += unkqryvars[1]
    print "Creating UNKTEMP table"
    cur.execute(unkqry0)
    print "Creating UNKFACTS table"
    cur.execute(unkqry1)

    print "Creating FULLOUTPUT table"
    # DONE: except we don't actually do it yet-- need to play with the variables and see the cleanest way to merge
    # the individual tables together
    # TODO: revise for consistent use of commas
    allsel = rdt('birth_date',dtcp)+""" birth_date, sex_cd 
      ,language_cd, race_cd, julianday(scaffold.start_date) - julianday("""+rdt('birth_date',dtcp)+") age_at_visit_days,"
    allsel += diagsel+','+loincsel+','+codesel+','+codemodsel+oneperdaysel+','+unkqryvars[0]
    allqry = "create table if not exists fulloutput as select scaffold.*,"+allsel
    allqry += """ from scaffold 
    left join diagfacts df on df.patient_num = scaffold.patient_num and df.start_date = scaffold.start_date
    left join loincfacts lf on lf.patient_num = scaffold.patient_num and lf.start_date = scaffold.start_date
    left join codefacts cf on cf.patient_num = scaffold.patient_num and cf.start_date = scaffold.start_date 
    left join codemodfacts cmf on cmf.patient_num = scaffold.patient_num and cmf.start_date = scaffold.start_date 
    left join oneperdayfacts one on one.patient_num = scaffold.patient_num and one.start_date = scaffold.start_date 
    left join unkfacts unk on unk.patient_num = scaffold.patient_num and unk.start_date = scaffold.start_date 
    left join patient_dimension pd on scaffold.patient_num = pd.patient_num
    order by patient_num, start_date"""
    cur.execute(allqry)

    print "Creating BINOUTPUT view of the result"
    binoutqry = """create view binoutput as select patient_num,start_date,birth_date,sex_cd
		   ,language_cd,race_cd,age_at_visit_days"""
    binoutqry += ","+",".join([" case when "+ii[1]+" is null then '"+binvals[0]+"' else '"+binvals[1]+\
			"' end "+ii[1] for ii in cnx.execute("pragma table_info(diagfacts)").fetchall()[2:]])
    binoutqry += ","+",".join([ii[1] for ii in cnx.execute("pragma table_info(loincfacts)").fetchall()[2:]])
    binoutqry += ","+",".join([" case when "+ii[1]+" is null then '"+binvals[0]+"' else '"+binvals[1]+\
			"' end "+ii[1] for ii in cnx.execute("pragma table_info(codefacts)").fetchall()[2:]])
    binoutqry += ","+",".join([" case when "+ii[1]+" is null then '"+binvals[0]+"' else '"+binvals[1]+\
			"' end "+ii[1] for ii in cnx.execute("pragma table_info(codemodfacts)").fetchall()[2:]])
    binoutqry += ","+",".join([ii[1] for ii in cnx.execute("pragma table_info(oneperdayfacts)").fetchall()[2:]])
    binoutqry += ","+",".join([" case when "+ii[1]+" is null then '"+binvals[0]+"' else '"+binvals[1]+\
			"' end "+ii[1] for ii in cnx.execute("pragma table_info(unkfacts)").fetchall()[2:]])
    binoutqry += " from fulloutput"
    cnx.execute("drop view if exists binoutput")
    cnx.execute(binoutqry)

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
    # DONE: write 'select * from fulloutput' to the csvfile. Should it be passed to main as a parameter? (yes)
    # TODO: create a view that replaces the various strings with simple 1/0 values
    print("--- %s seconds ---" % (time.time() - start_time))
    import pdb; pdb.set_trace()    
        
    # Boom! We covered all the cases. Messy, but at least a start.

    # the below yeah, I guess, but there are two big and easier to implement cases to do first


    """
    The decision process
      branch node
	uses mods DONE
	  map modifiers; single column of semicolon-delimited code=mod pairs
	uses other columns?
	  UNKNOWN FALLBACK, single column DONE
	code-only DONE
	  single column of semicolon-delimited codes
      leaf node
	code only DONE
	  single 1/0 column (TODO)
	uses code and mods only DONE
	  map modifiers; single column of semicolon-delimited mods DONE-ish
	uses other columns?
	  any columns besides mods have more than one value per patient-date?
	    UNKNOWN FALLBACK, single column DONE-ish
	  otherwise
	    map modifiers; single column of semicolon-delimited mods named FOO_mod; for each additional BAR, one more column FOO_BAR DONE-ish
    
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
      dtcp = 365.0/12
    else:
      dtcp = args.datecompress
      
    #import pdb; pdb.set_trace();
    #import code; code.interact(local=vars())
    if args.cleanup:
      cleanup(con)
    else:
      main(con,csvfile,args.style,dtcp)



