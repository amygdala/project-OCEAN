
# ‘entity extraction’ scripts

(Experimental work; in progress.)

The [`dataflow_names_emails.py`][1] script is a Beam/Dataflow pipeline that finds all the names associated with an email, and all the emails associated with a name, and outputs the results to two tables. 

The [`extract_entities.py`][2] script is an experiment in progress, to play with ways to identify the names and emails for a given person and aggregate them to create ‘entities’.  As an example of where this would be useful, see this query:

```sql
SELECT from_name, from_email
FROM `project-ocean-281819.mail_archives.names_emails`
WHERE REGEXP_CONTAINS(from_name, 'warsaw')
```

This is the query result— where not a given email is not associated with all given display names, and for a given display name, not all emails are used.
<figure>
<a href="https://storage.googleapis.com/amy-jo/images/Screen%20Shot%202020-09-18%20at%2011.18.26%20AM.png" target="_blank"><img src="https://storage.googleapis.com/amy-jo/images/Screen%20Shot%202020-09-18%20at%2011.18.26%20AM.png" width="40%"/></a>
<figcaption><br/><i><small>The same person may use many different email addresses and names.</small></i></figcaption>
</figure>

There are many such. We’d like to be able to identify messages with all of these different header configs as associated with the same person.

The entity extraction script dumps its results into a BQ table.


[1]:	./dataflow/dataflow_names_emails.py
[2]:	./extract_entities.py