# Copyright 2020 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import base64
from datetime import timezone
import email
import email.utils
import gzip
# import json
import os
import re
import time

import chardet
from dateutil import parser


from google.cloud import storage
from google.cloud import bigquery

ALLOWED_FIELDS = ['from', 'subject', 'date', 'message_id', 'in_reply_to', 'references',
      'body', 'list', 'to', 'cc', 'raw_date_string', 'body_bytes']
IGNORED_FIELDS = ['delivered_to', 'received', 'content_type', 'mime_version']


def blob_list(storage_client, bucketname, prefix):
  blobs = storage_client.list_blobs(
      bucketname, prefix=prefix, delimiter=None)
  return [blob.name for blob in blobs]

def chunks(l, n):
  """break a list into chunks"""
  for i in range(0, len(l), n):
    # Create an index range for l of n items:
    yield l[i:i+n]

def try_decode(string, additional_codecs=None):
  """try using various codecs in turn to decode a byte string"""

  codecs = ['utf8', 'iso8859_1', 'iso8859_2']
  if additional_codecs:
    codecs = additional_codecs + codecs
    # print('using codecs: {}'.format(codecs))
  exp = None
  for i in codecs:
    try:
      return string.decode(i)
    except (UnicodeDecodeError, LookupError) as e:
      exp = e
  print('cannot decode string {}'.format(string))
  raise exp

def get_msgs(storage_client, bucketname, fpath):
  """read a gcs file, and build an array of messages text.
  Uses the 'from' line after an empty line to detect a new message. (Which seems to work fine
  for our archives). Returns the array of messages.
  """
  bucket = storage_client.get_bucket(bucketname)

  blob = bucket.get_blob(fpath)
  # include timestamp in local file name to avoid archive name clashes in case we're processing
  # multiple buckets concurrently.
  lf = '/tmp/{}_{}'.format(int(time.time()), fpath)
  print('using local file: {}'.format(lf))

  # TODO: this will break if the 'filename' includes a path. Check for/create parent dir first.
  # (Our existing archive buckets don't have this issue)
  blob.download_to_filename(lf)

  msgs = []
  msg_lines = []
  try:
    with gzip.open(lf, 'rb') as f:
      for line in f:
        try:
          dl = try_decode(line)  # decode the line for re.search() call
        except UnicodeDecodeError as e:
          print(e)
          print(line)
          # TODO: this is unlikely to occur (it does not in our current archives), but what
          # is the right handling if it does?
          break
        m = re.search('^^From .*\d', dl)
        if m:
          msg = b''.join(msg_lines)
          msgs.append(msg)
          msg_lines = []
          msg_lines.append(line)
        else:
          msg_lines.append(line)
    if os.path.exists(lf):   # delete temp file
      os.remove(lf)
  except EOFError as e:
    print(e)
    # there appear to be some 'empty' archives for which this error will be generated.
    print('{} not successfully gunzipped'.format(fpath))
    time.sleep(5)
  return msgs


def get_email_objs(msgs):
  """parse the msg texts"""
  email_objs = []
  for m in msgs:
    if m:  # then parse the message.
      # TODO: is error-handling needed here? It doesn't appear to fail with the current archives.
      res = email.parser.BytesParser().parsebytes(m)
      email_objs.append(res)
  return email_objs


def get_msg_parts(msg):
  """given a parsed email (msg object), extract its header info and the text version of
  its body.
  """
  if msg.is_multipart():
    for part in msg.walk():
      ctype = part.get_content_type()
      cdispo = str(part.get('Content-Disposition'))
      if ctype == 'text/plain' and 'attachment' not in cdispo:
        body = part.get_payload(decode=True)
        break
  else:
    body = msg.get_payload(decode=True)
  mparts = msg.items()
  # currently, writing both the decoded string and the base64-ified bytestring for the body,
  # to bigquery. (Did this b/c I wasn't confident in how the decoding was happening in all cases.
  # However, I think it may be essentially redundant.)
  mparts.append(('Body', try_decode(body)))
  b64_bstring = base64.b64encode(body)
  b64_string = (str(b64_bstring)[2:])[:-1]  # uhhh... there has got to be a better way to convert
  # the base64 bytestring to a a string, needed for the json bq ingestion (but I couldn't figure
  # it out)
  mparts.append(('body_bytes', b64_string))
  return mparts


# TODO: this essentially works... but what's the best way to deal with all these different formats?
# (update: after discn on internal python chat channel, seems this may be the best approach...
# All the different formats are probably due to ancient mail client variants. It's the older
# messages that have issues.)
def parse_datestring(datestring):
  """given a date string, try to parse it into a date object."""
  date_object = None
  try:
    date_object = parser.parse(datestring)
  except parser._parser.ParserError:
    # print('date parsing error: {}'.format(err))
    datestring = datestring.replace('.', ':')  # arghh/hmmm
    # print('---- parsing: {}'.format(datestring))
    try:
      m = re.search('(.* [-+]\d\d\d\d).*$', datestring)
      # print('tried: {}'.format('(.* [-+]\d\d\d\d).*$'))
      # print('trying date string {}'.format(m[1]))
      date_object = parser.parse(m[1])
    except (TypeError, parser._parser.ParserError) as err2:
      print(err2)
      try:
        m = re.search('(.*)\(.*\)', datestring)
        # print('2nd try: {}'.format('(.*)\(.*\)'))
        # print('trying date string {}'.format(m[1]))
        date_object = parser.parse(m[1])
      except (TypeError, parser._parser.ParserError) as err3:
        print(err3)
        try:
          m = re.search('(.*) [a-zA-Z]+$', datestring)
          # print('3rd try: {}'.format('(.*) [a-zA-Z]+$'))
          # print('trying date string {}'.format(m[1]))
          date_object = parser.parse(m[1])
        except (TypeError, parser._parser.ParserError) as err4:
          print(err4)
          print('failed to strictly parse datestring {}; trying "fuzzy" parsing'.format(datestring))
          try:
            date_object = parser.parse(datestring, fuzzy=True)
          except parser._parser.ParserError:
            print('**********failed to parse datestring {}'.format(datestring))
  return date_object

# TODO: ughh, clean up this fcn
def get_email_dicts(parsed_msgs):
  """takes a list of message objects, and turns them into json dicts for insertion into BQ."""
  json_rows = []
  for msg in parsed_msgs:
    row_dict = {}
    row_dict['refs'] = []  # this repeated field is not nullable
    for e in msg:
      if e[0].lower() == 'date':  # convert to DATETIME-friendly utc time
        # store the raw string also, in case parsing issues, which happens in a few outlier cases
        try:
          row_dict['raw_date_string'] = e[1].strip()
          date_object = parse_datestring(e[1])
        except AttributeError as err:
          print('for "date", got error: {}'.format(err))
          dres = email.header.decode_header(e[1])
          # print('dres: {}'.format(dres))
          dres2 = try_decode(dres[0][0])
          print('dres2: {}'.format(dres2))
          row_dict['raw_date_string'] = dres2.strip()
          date_object = parse_datestring(dres2)
          time.sleep(10)
        if date_object:
          ds = date_object.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
          row_dict['date'] = ds
      elif e[0].lower() == 'from':
        dres = email.header.decode_header(e[1])  # decode header
        if isinstance(dres[0][0], bytes):
          print('dres: {}'.format(dres))
          dres_concat = b''
          enc = None
          for x in dres:
            dres_concat += x[0]
            if x[1] and not x[1] == 'unknown-8bit':
              enc = x[1]
              if enc == 'latin-2':
                enc = 'iso-8859-2'  # sigh
              print('**************got header enc: {}'.format(enc))
          if not enc:
            enc = chardet.detect(dres_concat)['encoding']
          if enc:
            dres2 = try_decode(dres_concat, additional_codecs=[enc])
          else:
            dres2 = try_decode(dres_concat)
          print('***for "from" {}: {}, got enc: {}\n with result {}'.format(e[1],
              dres_concat, enc, dres2))
          if dres2:
            from_string = dres2.lower().strip()
          else:  # hmmmm
            from_string = '{}'.format(e[1]).lower().strip()
        else:
          from_string = dres[0][0].lower().strip()
        row_dict['raw_from_string'] = from_string
        # the pipermail archives use the ' at ' syntax to encode the email addresses.
        from_addr = from_string.replace(' at ', '@')
        parsed_addr = email.utils.getaddresses([from_addr])
        # temp testing
        # if not parsed_addr[0][0]:
        #   print('---** problematic addr?')
        #   print('parsed_addr: {} from string {}'.format(parsed_addr, from_string))
        #   time.sleep(2)
        # TODO: better error checks/handling? The raw string will still be stored.
        if parsed_addr[0][0]:
          row_dict['from_name'] = parsed_addr[0][0]
        if parsed_addr[0][1]:
          row_dict['from_email'] = parsed_addr[0][1]

      elif e[0].lower() == 'references':
        try:
          refs_string = e[1].strip()
        except AttributeError as err:
          print('*******+++++++++++++++***********for {} got err: {}'.format(e[1], err))
          refs_string = '{}'.format(e[1])
          time.sleep(10)
        row_dict['references'] = refs_string
        # TODO: there seems to be a rare case where there's info in parens following a ref,
        # that prevents the regexp below from working properly. worth fixing?
        r1 = re.sub('>\s*<', '>|<', refs_string)
        refs = r1.split('|')
        # print('got refs: {}', refs)
        refs_record = [{"ref": x} for x in refs]
        row_dict['refs'] = refs_record

      else:  # for the rest of the fields
        # BQ fields allow underscores but not hyphens
        k = (e[0]).lower().replace('-', '_')
        if k in ALLOWED_FIELDS:
          try:
            row_dict[k] = e[1].strip()  # get rid of any leading/trailing whitespace
          except AttributeError as err:
            print('for *{}*, got error {} for {}'.format(k, err, e[1]))
            print('trying decode method...')
            dres = email.header.decode_header(e[1])
            print('dres: {}'.format(dres))
            enc = chardet.detect(dres[0][0])['encoding']
            print('got enc: {}'.format(enc))
            # TODO: do I need to handle substructure same as 'from' case above?
            if enc:
              dres2 = try_decode(dres[0][0], additional_codecs=[enc])
            else:
              dres2 = try_decode(dres[0][0])
            print('got decoded result: {}'.format(dres2))
            if dres2:
              row_dict[k] = dres2.strip()
            else:
              row_dict[k] = '{}'.format(e[1]).lower().strip()
            time.sleep(5)
        else:
          if k not in IGNORED_FIELDS:
            print('****ignoring unsupported message field: {} in msg {}'.format(k, e))
            time.sleep(2)

    json_rows.append(row_dict)
  return json_rows


def messages_to_bigquery(json_rows, table_id, chunk_size):
  """insert a list of message dicts into the given BQ table.  chunk_size determines how many
  are loaded at once. (If the payload is too large, it will throw an error.)
  """
  client = bigquery.Client()
  table = client.get_table(table_id)
  jrcs = chunks(json_rows, chunk_size)
  for jl in jrcs:
    errors = client.insert_rows_json(table, jl)
    if errors == []:
      print("New rows have been added without error.")
    else:
      print(errors)
      print(jl)
    time.sleep(1)


def main():
  argparser = argparse.ArgumentParser(description='BQ message ingestion')
  argparser.add_argument('--bucketname', required=True)
  argparser.add_argument('--chunk-size', type=int, default=200)
  # for testing; if set, will process just this file, which must exist in the given bucket
  argparser.add_argument('--filename')
  argparser.add_argument('--table-id',  # table_id = "your-project.your_dataset.your_table"
      default='project-ocean-281819.mail_archives.ingestion_test4')
  # include the '--ingest' flag to actually run the ingestion to BQ. Leave it out for testing.
  argparser.add_argument('--ingest', default=False, action='store_true')
  argparser.add_argument('--no-ingest', dest='ingest', action='store_false')
  args = argparser.parse_args()

  print('----using table: {}----'.format(args.table_id))
  time.sleep(10)
  storage_client = storage.Client()
  if args.filename:  # for testing: process just this file
    fnames = [args.filename]
  else:
    fnames = blob_list(storage_client, args.bucketname, None)  # not using a subdir prefix
  for filename in fnames:
    print('---------------')
    print('working on: {}'.format(filename))
    msgs = get_msgs(storage_client, args.bucketname, filename)
    if msgs:
      email_objs = get_email_objs(msgs)
      parsed_msgs = []
      for m in email_objs:
        mp = get_msg_parts(m)
        # get list name from bucketname. TODO: should this be its own explicit arg?
        mp.append(('list', args.bucketname.replace('-gzip', '')))
        parsed_msgs.append(mp)
      # Create list of json dicts from parsed email info
      json_rows = get_email_dicts(parsed_msgs)

      # temp testing
      with open('temp.oput', 'w') as f:
        for elt in json_rows:
          f.write('\n----------\n')
          f.write('{}'.format(elt))

      if args.ingest:
        # print('messages to bq')
        messages_to_bigquery(json_rows, args.table_id, args.chunk_size)
        time.sleep(1)
    else:
      print('*****no msgs obtained for {}'.format(filename))
      time.sleep(5)



if __name__ == "__main__":
  main()