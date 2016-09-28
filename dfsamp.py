""" Take a random sample of a DataBuilder file
---------------------------------------------------------------------
usage: dfsh.py [-h] [-n NSAMPLES] [-o OUTFILE] [dbin]
    
"""


import sqlite3 as sq,argparse,random,shutil
parser = argparse.ArgumentParser()
parser.add_argument("dbin",help="SQLite input file generated by DataBuilder")
parser.add_argument("-o","--outfile",help="SQLite output file",default="")
parser.add_argument("-n","--nsamples",help="Number or fraction of samples to keep",default=0.1,type=float)
parser.add_argument("-l","--log",help="Log verbose sql",action="store_true")
args = parser.parse_args()
dolog = args.log

def logged_execute(cnx, statement, comment=''):
    if dolog:
        if comment != '':
            print 'execute({0}): {1}'.format(comment, statement)
        else:
            print 'execute: {0}'.format(statement)
    return cnx.execute(statement)

def main(cnx,samples):
  import pdb;pdb.set_trace()
  if int(samples) != samples:
    npt = logged_execute(cnx,"select count(*) from patient_dimension").fetchone()[0]
    samples = int(round(samples*npt))
  else: samples = int(samples)
    
  pns = logged_execute(cnx,"select distinct patient_num from patient_dimension").fetchall()
  pns = random.sample(pns,samples)
  qry_selection = "create table selected as "+" union ".join(" select "+str(ii[0])+" pn " for ii in pns)
  logged_execute(cnx,qry_selection);
  qry_populate = "create table {0}_tmp as select {0}.* from {0} join selected on patient_num = pn"
  qry_delete = "delete from {0}"
  qry_insert = "insert into {0} select * from {0}_tmp"
  qry_drop = "drop table {0}_tmp"
  for ii in ['patient_dimension','observation_fact'] :
    logged_execute(cnx,qry_populate.format(ii))
    logged_execute(cnx,qry_delete.format(ii))
    logged_execute(cnx,qry_insert.format(ii))
    logged_execute(cnx,qry_drop.format(ii))
  logged_execute(cnx,'drop table selected')
  cnx.commit()
  logged_execute(cnx,'vacuum')

if __name__ == '__main__':
    outfile = args.outfile
    if outfile=="":
      outfile = "sample_"+args.dbin
    shutil.copyfile(args.dbin,outfile)
    con = sq.connect(outfile)
    main(con,args.nsamples)
