""" Generate dynamic data extraction SQL for DataBuilder output files
---------------------------------------------------------------------
    
 Usage:
   makesql sqltemplate.sql dbname.db
"""

import sqlite3 as sq,argparse

parser = argparse.ArgumentParser()
parser.add_argument("dbfile",help="SQLite file generated by DataBuilder")
args = parser.parse_args()

# location of data dictionary sql file
ddsql = "sql/dd.sql"


def main(dbfile):
    con = sq.connect(dbfile)
    cur = con.cursor()
    # TODO (ticket #1): instead of relying on sqlite_denorm.sql, create the scaffold table from inside this 
    # script by putting the appropriate SQL commands into character strings and then passing those
    # strings as arguments to execute() (see below for an example of cur.execute() usage (cur just happens 
    # to be what we named the cursor object we created above, and execute() is a method that cursor objects have)
    # DONE: create an id to concept_cd mapping table (and filtering out redundant facts taken care of here)
    # TODO: parameterize the fact-filtering
    
    print "Creating scaffold table"
    cur.execute("drop table if exists scaffold")
    cur.execute("""
    CREATE TABLE
    scaffold
    (
        patient_num NUM,
        start_date DATE
    );""")
    cur.execute("""insert into scaffold (patient_num, start_date) 
    select distinct patient_num, date(start_date) start_date
    from observation_fact order by patient_num, start_date;
    """)

    print "Creating CDID table"
    cur.execute("drop table if exists cdid")
    cur.execute("""
	create table cdid as
	select distinct concept_cd ccd,substr(concept_cd,1,instr(concept_cd,':')-1) ddomain,id from concept_dimension cd 
	join (select min(id) id,min(concept_path) concept_path 
	from variable 
	where name not like '%old at visit' and name not in ('Living','Deceased','Not recorded','Female','Male','Unknown')
	group by item_key) vr
	on cd.concept_path like vr.concept_path||'%'
	""")
    # create a couple of cleaned-up views of observation_fact
    # replace most of the non-informative values with nulls, remove certain known redundant modifiers
    print "Creating obs_all and obs_noins views"
    cur.execute("drop view if exists obs_all")
    cur.execute("""
	create view obs_all as
	select distinct patient_num,concept_cd,date(start_date) start_date,modifier_cd
	,case when valtype_cd in ('@','') then null else valtype_cd end valtype_cd
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
	select distinct patient_num,concept_cd,date(start_date) start_date,modifier_cd
	,case when valtype_cd in ('@','') then null else valtype_cd end valtype_cd
	,case when tval_char in ('@','') then null else tval_char end tval_char
	,nval_num
	,case when valueflag_cd in ('@','') then null else valueflag_cd end valueflag_cd
	,quantity_num
	,units_cd,location_cd,confidence_num from observation_fact
	where modifier_cd not in ('Labs|Aggregate:Last','Labs|Aggregate:Median','PROCORDERS:Outpatient','DiagObs:PROBLEM_LIST')
	and concept_cd not like 'DEM|AGEATV:%' and concept_cd not like 'DEM|SEX:%' and concept_cd not like 'DEM|VITAL:%'
        ) group by patient_num,concept_cd,start_date,modifier_cd,units_cd""");
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
    cur.execute("""
        update data_dictionary set rule = 'code' where
        coalesce(mod,tval_char,valueflag_cd,units_cd,confidence_num,quantity_num,location_cd,valtype_cd,nval_num,-1) = -1
        """)
    cur.execute("""
        update data_dictionary set rule = 'codemod' where
        coalesce(tval_char,valueflag_cd,units_cd,confidence_num,quantity_num,location_cd,valtype_cd,nval_num,-1) = -1
        and mod is not null""")
    cur.execute("update data_dictionary set rule = 'oneperday' where mxfacts = 1 and rule = 'UNKNOWN_DATA_ELEMENT'")
    con.commit()
    
    print "Creating dynamic SQL for CODEFACTS"
    cur.execute("select group_concat(colid) from data_dictionary where rule = 'code'")
    codesel = cur.fetchone()[0]
    # dynamically generate the terms in the select statement
    # extract the terms that meet the above criterion
    codeqry = "create table if not exists codefacts as select scaffold.*,"+codesel+" from scaffold "
    # now dynamically generate the many, many join clauses and append them to codefacts
    # note the string replace-- cannot alias the table name in an update statement, so no dd
    cur.execute("""
	select ' left join (select patient_num,date(start_date) sd
	,replace(group_concat(distinct concept_cd),'','',''; '') '||colid||' from cdid 
	join obs_noins on ccd = concept_cd where id = '||cid||' group by patient_num
        ,date(start_date) order by patient_num,start_date) '||colid||' 
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
        select ' left join (select patient_num,date(start_date) sd
        ,replace(group_concat(distinct concept_cd||''=''||modifier_cd),'','',''; '') '||colid||' from cdid 
        join obs_noins on ccd = concept_cd where id = '||cid||' group by patient_num
        ,date(start_date) order by patient_num,start_date) '||colid||' 
        on '||colid||'.patient_num = scaffold.patient_num 
        and '||colid||'.sd = scaffold.start_date' from data_dictionary where rule = 'codemod'""")
    codemodqry += " ".join([row[0] for row in cur.fetchall()])
    print "Creating CODEMODFACTS table"
    cur.execute(codemodqry)
    
    # any variable that doesn't have multiple values on the same day 
    # (except multiple instances of numeric variables which get averaged)
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
    
    print "Creating dynamic SQL for UNKTEMP and UNKFACTS tables"
    cur.execute("""select group_concat(colid),
	group_concat('left join (select patient_num pn,start_date sd,megacode '||colid||
	    ' from unktemp where id = '||cid||') '||colid||' on '||colid||'.pn = patient_num 
	      and '||colid||'.sd = start_date ',' '),
	group_concat(cid) from data_dictionary where rule = 'UNKNOWN_DATA_ELEMENT'""")
    unkqryvars = cur.fetchone()
    unkqry0 = """create table if not exists unktemp as 
	select patient_num,date(start_date) start_date,id
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
    allsel = """date(birth_date) birth_date, sex_cd 
      ,language_cd, race_cd, julianday(start_date) - julianday(birth_date) age_at_visit_days,""" + codesel+','+codemodsel+oneperdaysel+','+unkqryvars[0]
    allqry = "create table if not exists fulloutput as select scaffold.*,"+allsel
    allqry += """ from scaffold 
    left join codefacts cf on cf.patient_num = scaffold.patient_num and cf.start_date = scaffold.start_date 
    left join codemodfacts cmf on cmf.patient_num = scaffold.patient_num and cmf.start_date = scaffold.start_date 
    left join oneperdayfacts one on one.patient_num = scaffold.patient_num and one.start_date = scaffold.start_date 
    left join unkfacts unk on unk.patient_num = scaffold.patient_num and unk.start_date = scaffold.start_date 
    left join patient_dimension pd on scaffold.patient_num = pd.patient_num
    order by patient_num, start_date"""
    cur.execute(allqry)
    
    
    import pdb; pdb.set_trace()    
    
    
    # TODO: create a view that replaces the various strings with simple 1/0 values
    
    
    # Boom! We covered all the cases. Messy, but at least a start.

    # the below yeah, I guess, but there are two big and easier to implement cases to do first
    # 1. code-only can be the same use-case for branches and leaves, the result will be the same
    # 2. code-mod-only slightly uglier if doesn't distinguish between branches and leaves but will
    #    work well enough for now 

    # DONE: cid's (column id's i.e. groups of variables that were selected together by the researcher)
    # ...cid's that have a ccd value of 1 (meaning there is only one distinct concept code per cid
    # since these get checked after the code and code-mod groups, these are expected to be numeric variables
    # after these are dealt with,

    # next next target: fallback on giant messy concatenated strings for everything else (for now)
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
    TODO: The data dictionary will contain information about which built-in or user-configured rule applies for each vid
    We are probably looking at several different 'dcat' style tables, broken up by type of data
    TODO: We will iterate through the data dictionary, joining new columns to the result according to the applicable rule
    """
    
    
	
if __name__ == '__main__':
    main(args.dbfile)



