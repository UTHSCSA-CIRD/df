# datafinisher
A script to post-process DataBuilder output into a single analyzable, denormalized table or spreadsheet. I.e. one row per patient-day, one column for each data element that will be treated as a separate variable during statistical analysis (with some accompanying columns for units, modifiers, various flags/comments). Uses lots of dynamic SQL, but all of it within sqlite3

Note: This code does not require require connections to any external database or any other service. It runs entirely upon the files you supply to it. 

Note: This code modifies the original databuilder .db file, but only adds tables, doesn't modify existing ones with the exception of empty tables.

<code>
    usage: df.py [-h] [-l] [-c] [-v CSVFILE] [-s {concat,simple}] [-d DATECOMPRESS] dbfile
  
    positional arguments:
    dbfile                SQLite file generated by DataBuilder
  
    optional arguments:
      -h, --help            show this help message and exit
      -l, --log             Log verbose sql
      -c, --cleanup         Restore dbfile to its vanilla, data-builder state
      -v CSVFILE, --csvfile CSVFILE
                          File to write output to, in addition to the tables that will get created in the  dbfile. By default this is whatever was the name of the dbfile with '.csv' substituted for '.db'
      -s {concat,simple}, --style {concat,simple}
                          What style to output the file, currently there are two-- concat which concatenates the code variables and simple which represents code variables as Yes/No, with the nulls represented by No. The default is concat.
      -d DATECOMPRESS, --datecompress DATECOMPRESS
                          Round all dates to the nearest X days, default is 1
</code>

Here are the functional parts:

* **df.py**                The part you run
* **df_fn.py**                Declarations of functions and other stuff used by df.py
* **ruledefs.csv**            Customizable rules file
* **sql/**                    SQL scripts and data used by df
 * **sql/datafinisher.db**     A SQLite db with some lookup tables, at the moment it contains only MODIFIER_DIMENSION (used only if the one in the input file is empty)
 * **sql/dd.sql**              A script for creating the DATA_DICTIONARY table
 * **sql/df.cfg**              The config file, which includes many snippets of SQL that are called from various places in df.py

Thanks, have a nice day!
