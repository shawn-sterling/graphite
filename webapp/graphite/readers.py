import os
import time
from graphite.node import LeafNode, BranchNode
from graphite.intervals import Interval, IntervalSet
from graphite.carbonlink import CarbonLink
from graphite.logger import log


try:
  import whisper
except ImportError:
  whisper = False

try:
  import rrdtool
except ImportError:
  rrdtool = False

try:
  import gzip
except ImportError:
  gzip = False



class MultiReader:
  def __init__(self, nodes):
    self.nodes = nodes

  def get_intervals(self):
    interval_sets = [ n.intervals for n in self.nodes ]
    return reduce(IntervalSet.union, interval_sets)

  def fetch(self, startTime, endTime): #TODO allow for parallelism in RemoteReader.fetch() calls (threads?)
    results = [ n.fetch(startTime, endTime) for n in self.nodes ]
    return reduce(self.merge, results)

  def merge(self, results1, results2):
    # Ensure results1 is finer than results2
    if results1[0][2] > results2[0][2]:
      results1, results2 = results2, results1

    time_info1, values1 = results1
    time_info2, values2 = results2
    start1, end1, step1 = time_info1
    start2, end2, step2 = time_info2

    step   = step1                # finest step
    start  = min(start1, start2)  # earliest start
    end    = max(end1, end2)      # latest end
    time_info = (start, end, step)
    values = []

    t = start
    while t < end:
      # Look for the finer precision value first if available
      i1 = (t - start1) / step1

      if len(values1) > i1:
        v1 = values1[i1]
      else:
        v1 = None

      if v1 is None:
        i2 = (t - start2) / step2

        if len(values2) > i2:
          v2 = values2[i2]
        else:
          v2 = None

        values.append(v2)
      else:
        values.append(v1)

      t += step

    return (time_info, values)


class CeresReader:
  supported = True

  def __init__(self, ceres_node, real_metric_path):
    self.ceres_node = ceres_node
    self.real_metric_path = real_metric_path

  def get_intervals(self):
    intervals = []
    for info in self.ceres_node.slice_info:
      (start, end, step) = info
      intervals.append( Interval(start, end) )

    return IntervalSet(intervals)

  def fetch(self, startTime, endTime):
    data = self.ceres_node.read(startTime, endTime)
    time_info = (data.startTime, data.endTime, data.timeStep)
    values = list(data.values)

    # Merge in data from carbon's cache
    if data.endTime < endTime:
      try:
        cached_datapoints = CarbonLink.query(self.real_metric_path)
      except:
        log.exception("Failed CarbonLink query '%s'" % self.real_metric_path)
        cached_datapoints = []

      for (timestamp, value) in cached_datapoints:
        interval = timestamp - (timestamp % data.timeStep)

        try:
          i = int(interval - data.startTime) / data.timeStep
          values[i] = value
        except:
          pass

    return (time_info, values)


class WhisperReader:
  supported = bool(whisper)

  def __init__(self, fs_path):
    self.fs_path = fs_path

  def get_intervals(self):
    start = time.time() - whisper.info(self.fs_path)['maxRetention']
    end = os.stat(self.fs_path).st_mtime
    return IntervalSet( [Interval(start, end)] )

  def fetch(self, startTime, endTime):
    return whisper.fetch(self.fs_path, startTime, endTime)


class GzippedWhisperReader(WhisperReader):
  supported = bool(whisper and gzip)

  def get_intervals(self):
    fh = gzip.GzipFile(self.fs_path, 'rb')
    try:
      info = whisper.__readHeader(fh) # evil, but necessary.
    finally:
      fh.close()

    start = time.time() - info['maxRetention']
    end = os.stat(self.fs_path).st_mtime
    return IntervalSet( [Interval(start,end)] )

  def fetch(self, startTime, endTime):
    fh = gzip.GzipFile(self.fs_path, 'rb')
    try:
      return whisper.file_fetch(fh, startTime, endTime)
    finally:
      fh.close()


class RRDReader:
  supported = bool(rrdtool)

  def __init__(self, fs_path, datasource_name):
    self.fs_path = fs_path
    self.datasource_name = datasource_name

  def get_intervals(self):
    start = time.time() - self.get_retention(self.fs_path)
    end = os.stat(self.fs_path).st_mtime
    return IntervalSet( [Interval(start, end)] )

  def fetch(self, startTime, endTime):
    startString = time.strftime("%H:%M_%Y%m%d", time.localtime(startTime))
    endString = time.strftime("%H:%M_%Y%m%d", time.localtime(endTime))

    (timeInfo, columns, rows) = rrdtool.fetch(self.fs_path,'AVERAGE','-s' + startString,'-e' + endString)
    colIndex = list(columns).index(self.datasource_name)
    rows.pop() #chop off the latest value because RRD returns crazy last values sometimes
    values = (row[colIndex] for row in rows)

    return (timeInfo, values)

  @staticmethod
  def get_datasources(fs_path):
    info = rrdtool.info(fs_path)

    if 'ds' in info:
      return [datasource_name for datasource_name in info['ds']]
    else:
      ds_keys = [ key for key in info if key.startswith('ds[') ]
      datasources = set( key[3:].split(']')[0] for key in ds_keys )
      return list(datasources)

  @staticmethod
  def get_retention(fs_path): #FIXME probly won't work with the old rrdtool API
    info = rrdtool.info(fs_path)
    rows = {}
    pdp_per_row = {}

    for key in info:
      if key.startswith('rra[') and key.endswith('].rows'):
        id = key[4:-6]
        rows[id] = info[key]

      elif key.startswith('rra[') and key.endswith('].pdp_per_row'):
        id = key[4:-13]
        pdp_per_row[id] = info[key]

    return info['step'] * max( rows[id] * pdp_per_row[id] for id in rows )