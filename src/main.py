#!/bin/env python3

import feedparser
import argparse
import os
from os import path, getcwd
import toml
import xml.etree.ElementTree as ET
from io import IOBase
import json
import requests
import time
from configparser import ConfigParser
import re
import logging

# ----- ----- ----- ----- -----

class podqueue():


  def __init__(self):
    # Initialise to defaults before checking config file / CLI args
    self.verbose = False
    self.opml = None
    self.dest = os.path.join(os.getcwd(), 'output')
    self.time_format = '%Y-%m-%d'
    self.log_file = 'podqueue.log'
    self.feeds = []
    self.FEED_FIELDS = ['title', 'link', 'description', 'published', 'image', 'categories',]
    self.EPISODE_FIELDS = ['title', 'link', 'description', 'published_parsed', 'links',]

    # If a config file exists, ingest it
    self.check_config()

    # Overwrite any config file defaults with CLI params
    self.cli_args()

    self.config_logging()

    # Check an OPML was provided
    try:
      assert self.opml is not None
    except Exception as e:
      logging.exception('OPML file or destination dir was not provided')
      exit()


  def config_logging(self):

    # Always log to file; only stdout if -v
    handlers = [logging.FileHandler(self.log_file)]
    if (self.verbose): handlers.append(logging.StreamHandler())

    # Config settings
    level = logging.INFO if (self.verbose) else logging.WARNING
    logging.basicConfig(level=level, datefmt='%Y-%m-%d %H:%M:%S', handlers=handlers,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    # Add header for append-mode file logging
    logging.info('\n----- ----- ----- ----- -----\nInitialising\n----- ----- ----- ----- -----')


  def ascii_normalise(self, input_str):
    try:
      input_str = re.sub(r'[^a-zA-Z0-9\-\_\/\\\.]', '_', input_str)
    except Exception as e:
      logging.exception(f'\t\tError normalising file name: {e}')
      exit()

    return input_str


  def check_config(self):
    # get the path to config.ini
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

    # Check if the file has been created
    if not os.path.exists(config_path):
      logging.info(f'Config file does not exist: {config_path}')
      return None

    conf = ConfigParser()
    conf.read(config_path)

    for key in ['opml', 'dest', 'time_format', 'verbose', 'log_file']:
      if conf['podqueue'].get(key, None):
        setattr(self, key, conf['podqueue'].get(key, None))

    # If we just changed verbose to str, make sure it's back to a bool
    if self.verbose:
      self.verbose = bool(self.verbose)
    
    return


  def cli_args(self):
    parser = argparse.ArgumentParser(add_help=True)

    parser.add_argument('-o', '--opml', dest='opml', default=None, type=argparse.FileType('r'),
      help='Pass an OPML file that contains a podcast subscription list.')
    parser.add_argument('-d', '--dest', dest='dest', type=self.args_path,
      help='The destination folder for downloads. Will be created if required, including sub-directories for each separate podcast.')
    parser.add_argument('-t', '--time_format', dest='time_format',
      help='Specify a time format string for JSON files. Defaults to 2022-06-31 if not specified.')
    parser.add_argument('-v', '--verbose', default=False, action='store_true',
      help='Prints additional debug information. If excluded, only errors are printed (for automation).')
    parser.add_argument('--log_file', dest='log_file',
      help='Specify a path to the log file. Defaults to ./podqueue.log')
    
    # Save the CLI args to class vars - self.XXX
    # vars() converts into a native dict
    result = vars(parser.parse_args())
    for key, value in result.items():
      # Don't overwrite if it's not provided in CLI
      if value is not None:
        setattr(self, key, value)


  def args_path(self, directory):
    # Create the directory, if required
    if  not os.path.isdir(directory):
      os.makedirs(directory)

    return directory


  def parse_opml(self, opml):
    logging.info(f'Parsing OPML file: {opml}')

    # Check if we have an actual file handle (CLI arg), 
    # Or a string path (config file), and we need to get our own handle
    with (opml if isinstance(opml, IOBase) else open(opml, 'r')) as opml_f:
      xml_root = ET.parse(opml_f).getroot()

    # Get all RSS feeds with a 'xmlUrl' attribute
    for feed in [x.attrib for x in xml_root.findall("*/outline/*[@type='rss']")]:
      feed_url = feed.get('xmlUrl', None)
      if feed_url:
        self.feeds.append(feed_url)


  def get_feeds(self, feeds):
    logging.info(f'Fetching feeds:')

    for feed in feeds:
      content = feedparser.parse(feed)

      # If feedparser library reports bad XML, warn and skip
      # Test str: 'http://feedparser.org/tests/illformed/rss/aaa_illformed.xml'
      if content.get('bozo', False):
        logging.warning(f'Feed is misformatted: {feed}')
        continue

      logging.info(f'\tProcessing feed: {content.feed.title}')

      # Normalise the podcast name with no spaces or non-simple ascii
      feed_dir_name = '_'.join([x for x in content.feed.title.split(' ')])
      feed_dir_name = self.ascii_normalise(feed_dir_name)

      # Create the directory we need (no spaces) if it doesn't exist
      directory = os.path.join(self.dest, feed_dir_name)
      if not os.path.isdir(directory):
        os.makedirs(directory)

      # Also create the <<PODCAST>>/episodes subdirectory
      if not os.path.isdir(os.path.join(directory, 'episodes')):
        os.makedirs(os.path.join(directory, 'episodes'))

      # Get content.feed metadata - podcast title, icon, description, etc.
      # And write it to disk as <<PODCAST>>/<<PODCAST>>.json
      feed_metadata = self.process_feed_metadata(content, directory)

      # Also fetch the podcast logo, if available
      if feed_metadata.get('image', None):
        self.get_feed_image(feed_metadata['image'], directory)

      # Then, process the episodes each and write to disk
      for episode in content.entries:
        episode_data = self.process_feed_episode(episode, directory)

    return None


  def process_feed_metadata(self, content, directory):
    logging.info(f'\t\tProcessing feed metadata')

    feed_metadata = {}

    for field in self.FEED_FIELDS:
      # .image is a dict structure where we only want href, 
      # the rest are strs, so special case
      if (field == 'image') and (content.feed.get('image', None)):
        value = content.feed.image.href
      else:
        value = content.feed.get(field, None)

      feed_metadata[field] = value

    # Additional calculated metadata based on structure:
    feed_metadata['episode_count'] = len(content.entries)

    metadata_filename = os.path.join(directory, f'{os.path.split(directory)[1]}.json')
    metadata_filename = self.ascii_normalise(metadata_filename)
    with open(metadata_filename, 'w') as meta_f:
      meta_f.write(json.dumps(feed_metadata))

    return feed_metadata


  def get_feed_image(self, image_url, directory):

    img = requests.get(image_url)

    # Check successful
    try:
      img.raise_for_status()
    except requests.exceptions.HTTPError as e:
      logging.warning(f'\t\tImage could not be found: {image_url}')
      return

    image_filename_ext = os.path.splitext(image_url)[1]
    image_filename = os.path.join(directory, f'{os.path.split(directory)[1]}{image_filename_ext}')
    image_filename = self.ascii_normalise(image_filename)

    with open(image_filename, 'wb') as img_f:
      for chunk in img.iter_content(chunk_size=128):
        img_f.write(chunk)

    logging.info(f'\t\tAdded image to disk: {os.path.split(image_filename)[1]}')
    return


  def process_feed_episode(self, episode, directory):

    episode_metadata = {}
    for field in self.EPISODE_FIELDS:
      episode_metadata[field] = episode.get(field, None)

    # Change the time_struct tuple to a human string
    if episode_metadata.get('published_parsed', None):
      episode_metadata['published_parsed'] = time.strftime(self.time_format, \
                                            episode_metadata['published_parsed'])

    # Change the links{} into a single audio URL
    if episode_metadata.get('links', None):
      for link in episode_metadata['links']:
        if 'audio' in link['type']:
          episode_metadata['link'] = link['href']
          break

      # Remove the old complicated links{}
      episode_metadata.pop('links', None)

    # Figure out a unique episode filename(s)
    episode_title = '_'.join([x for x in episode_metadata['title'].split(' ')])
    episode_meta_filename = os.path.join(os.path.join(directory, 'episodes'), \
                        f'{episode_metadata["published_parsed"]}_{episode_title}.json')
    episode_audio_filename = os.path.join(os.path.join(directory, 'episodes'), \
                        f'{episode_metadata["published_parsed"]}_{episode_title}.mp3')

    episode_meta_filename = self.ascii_normalise(episode_meta_filename)
    episode_audio_filename = self.ascii_normalise(episode_audio_filename)

    # Check if the file already exists on disk (if so, skip)
    if os.path.exists(episode_meta_filename) and os.path.exists(episode_audio_filename):
      logging.info(f'\t\tEpisode already saved, skipping: {episode_title}')
      return

    # Write metadata to disk
    with open(episode_meta_filename, 'w') as ep_meta_f:
      ep_meta_f.write(json.dumps(episode_metadata))
    logging.info(f'\t\t\tAdded episode metadata to disk: {episode_title}')

    # Download the audio file
    if episode_metadata['link']:
      audio = requests.get(episode_metadata['link'])

    # Check successful
    try:
      audio.raise_for_status()
    except requests.exceptions.HTTPError as e:
      logging.warning(f'\t\t\tAudio could not be found: {episode_metadata["link"]}')
      return

    # Write audio to disk
    with open(episode_audio_filename, 'wb') as audio_f:
      for chunk in audio.iter_content(chunk_size=128):
        audio_f.write(chunk)
    logging.info(f'\t\t\tAdded episode audio to disk: {episode_title}')

    return


# ----- ----- ----- ----- -----

def entry():
  # Initialise the config - from file, or CLI args
  pq = podqueue()
  
  # Parse all feed URLs out of the OPML XML into pq.feeds=[]
  pq.parse_opml(pq.opml)

  # Download the metadata, images, and any missing episodes
  pq.get_feeds(pq.feeds)


if __name__ == '__main__':
  entry()
