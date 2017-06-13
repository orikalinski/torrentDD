#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re
import os
import time
import argparse
from textblob.blob import TextBlob
import zipfile
import StringIO
import Levenshtein
from enum import Enum

import dryscrape
import requests
import transmissionrpc
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from requests.adapters import HTTPAdapter

SIMILARITY_THRESHOLD = 0.75
SEEKER_THRESHOLD = 50
LEECHES_THRESHOLD = 5
MAX_NUMBER_OF_EPISODE_PER_SEASON = 20

MAGNET_REGEX = re.compile("^magnet")
DOWNLOAD_REGEX = re.compile("\?(.*?)'")
VERSION_REGEX_PATTERN = "%s\.(.+?)(?:\.mkv)?$"

Status = Enum('Status', 'success no_connection no_results generic_error')


class BaseDownloader(object):
    def __init__(self):
        self.user_agent = UserAgent()

    @staticmethod
    def get_episode_name(series, season_number, episode_number):
        return "{series}.s{season}e{episode}".format(series=series.replace(' ', '.'), season=season_number,
                                                     episode=episode_number)


class MoviesDownloader(BaseDownloader):
    def __init__(self):
        super(MoviesDownloader, self).__init__()
        self.session = requests.Session()
        self.tc = transmissionrpc.Client(user="transmission", password="transmission")

    def set_crawling_attributes(self):
        print "Setting movies crawler attributes"
        headers = requests.utils.default_headers()
        headers.update({'User-Agent': self.user_agent.random})
        self.session.headers = headers
        self.session.mount("https://", HTTPAdapter(max_retries=5))

    def get_pirate_bay_soup(self, episode):
        pirate_url = "https://thepiratebay.org/search/{episode}/0/99/0".format(episode=episode)
        print "Trying to reach: %s" % pirate_url
        response = self.session.get(pirate_url)
        if response.ok:
            soup = BeautifulSoup(response.content, "lxml")
        else:
            print "Failed while trying to fetch html from: %s" % pirate_url
            return
        return soup

    def extract_magnet_link_from_soup(self, soup, episode):
        print "Extracting magnet link for episode: %s" % episode
        data = self.extract_results(soup)
        magnet_link = self.find_best_result(data, episode)
        return magnet_link

    @staticmethod
    def extract_results(soup):
        data = []
        table = soup.find('table', attrs={'id': 'searchResult'})

        if table:
            rows = table.find_all('tr')
            for row in rows[1:]:
                cols = row.find_all('td')
                cols_text = [ele.text.strip() for ele in cols]
                cols_text.extend([None, None])
                for col in cols:
                    magnet_link = col.find('a', href=MAGNET_REGEX)
                    is_valid = col.find(attrs={'title': ['VIP', 'Trusted']}) is not None
                    if is_valid:
                        cols_text[5] = is_valid
                    if magnet_link:
                        cols_text[4] = magnet_link.get('href')
                data.append(cols_text)
        return data

    @staticmethod
    def find_best_result(data, episode):
        for row in data:
            _, name, se, le, magnet_link, is_valid = row
            if magnet_link and (episode in name.lower() or episode.replace('.', ' ') in name.lower()) \
                and ((int(se) > SEEKER_THRESHOLD and int(le) > LEECHES_THRESHOLD)
                     or (is_valid and int(se) > SEEKER_THRESHOLD // 5)):
                print u"Found {trusted} result: {name} with {seeders} seeders, " \
                      u"{leechers} leechers".format(name=name, seeders=se, leechers=le,
                                                    trusted="trusted" if is_valid else "not trusted")
                return magnet_link

    def download_torrent_from_magnet_link(self, magnet_link, download_directory):
        torrent = self.tc.add_torrent(magnet_link, download_dir=download_directory)
        while not hasattr(torrent, "status") or torrent.status == "downloading":
            time.sleep(5)
            torrent.update()
            print "Downloading torrent, progress: %s" % torrent.progress
        print "Done downloading the link"
        return torrent.name

    def download_torrent(self, series, season_number, episode_number, download_directory):
        episode = self.get_episode_name(series, season_number, episode_number)
        self.set_crawling_attributes()
        soup = self.get_pirate_bay_soup(episode)
        if soup:
            magnet_link = self.extract_magnet_link_from_soup(soup, episode)
            if magnet_link:
                download_name = self.download_torrent_from_magnet_link(magnet_link, download_directory)
                if download_name:
                    download_version = re.search(VERSION_REGEX_PATTERN % episode, download_name, re.I).group(1)
                    return Status.success, download_version
            else:
                print "Couldn't find any matching episode to: %s" % episode
                return Status.no_results, None
        else:
            return Status.no_connection, None
        return Status.generic_erorr, None


class SubtitlesDownloader(BaseDownloader):
    def __init__(self):
        super(SubtitlesDownloader, self).__init__()
        self.session = dryscrape.Session()

    def set_crawling_attributes(self):
        print "Setting subtitles crawler attributes"
        self.session.set_header('User-Agent', self.user_agent.random)

    @staticmethod
    def get_hebrew_episode_name(series, season_number, episode_number):
        blob = TextBlob(series)
        hebrew_series_name = blob.translate(to="he")
        res = raw_input(u"Did you mean: %s? (Y/n)" % hebrew_series_name)
        if res == 'n':
            hebrew_series_name = raw_input(u"Write the hebrew series name")
        hebrew_episode = u"{hebrew_series_name} עונה {season_number} פרק {episode_number}" \
            .format(hebrew_series_name=hebrew_series_name, season_number=season_number, episode_number=episode_number)
        return hebrew_episode

    def get_subscenter_soup(self, series, season_number, episode_number):
        subscenter_url = "http://www.subscenter.org/he/subtitle/series/{series}/{season_number}/{episode_number}/" \
            .format(series=series.replace(' ', '-'), season_number=season_number, episode_number=episode_number)
        print "Trying to reach: %s" % subscenter_url
        self.session.visit(subscenter_url)
        try:
            self.session.wait_for(lambda: self.session.at_css("div.subsDownloadVersion"))
        except Exception, e:
            print "Failed while trying to download the subtitles: %s" % e
            return None
        response = self.session.body()
        soup = BeautifulSoup(response, "lxml")
        return soup

    @staticmethod
    def get_download_link(soup, episode, download_version):
        print "Extracting download link for episode: %s" % episode
        buttons_text = soup.find_all("div", {"class": "subsDownloadBtn"})
        versions = soup.find_all("div", {"class": "subsDownloadVersion"})
        download_id = None
        final_download_version = None
        for button_text, version in zip(buttons_text, versions):
            result = re.search(VERSION_REGEX_PATTERN % episode, version.text, re.I)
            if result:
                version = result.group(1)
                if not download_id \
                        or Levenshtein.ratio(version.lower(), download_version.lower()) > SIMILARITY_THRESHOLD:
                    final_download_version = version
                    download_id = DOWNLOAD_REGEX.search(button_text.find("a").get("onclick")).group(1)
        if download_id:
            download_link = "http://www.subscenter.org/he/get/download/he/?{download_id}"\
                .format(download_id=download_id)
            print "Found subtitles of version: %s" % final_download_version
            return download_link
        print "Couldn't find any matching subtitles to: %s" % episode

    @staticmethod
    def stream_download_subtitles(download_link, download_directory):
        response = requests.get(download_link, stream=True)
        if response.ok:
            z = zipfile.ZipFile(StringIO.StringIO(response.content))
            z.extractall(download_directory)
            print "Subtitles extracted in: %s" % download_directory
        else:
            print "Failed while trying to download the subtitles"

    def download_subtitles(self, series, season_number, episode_number, download_version, download_directory):
        episode = self.get_episode_name(series, season_number, episode_number)
        self.set_crawling_attributes()
        soup = self.get_subscenter_soup(series, season_number, episode_number)
        if soup:
            download_link = self.get_download_link(soup, episode, download_version)
            self.stream_download_subtitles(download_link, download_directory)


def create_directory(directory):
    if not os.path.exists(directory):
        os.mkdir(directory)
        os.chmod(directory, 0777)


def run(series, season_number, episode_number, download_directory, **kwargs):
    season_number = str(season_number).zfill(2)
    if episode_number:
        episodes_numbers = [episode_number]
    else:
        episodes_numbers = range(1, MAX_NUMBER_OF_EPISODE_PER_SEASON)

    # hebrew_series_name = args.hebrew_series_name
    # hebrew_episode_name = subtitles_downloader.get_hebrew_episode_name(series, season_number, episode_number)

    for component in [series.replace(' ', '-'), "season%s" % season_number]:
        download_directory = os.path.join(download_directory, component)
        create_directory(download_directory)
    movies_downloader = MoviesDownloader()
    subtitles_downloader = SubtitlesDownloader()

    downloaded_episodes = list()
    for episode_number in episodes_numbers:
        status = None
        while not status or status == Status.no_connection:
            episode_number = str(episode_number).zfill(2)
            episode_download_directory = os.path.join(download_directory, "episode%s" % episode_number)
            create_directory(episode_download_directory)
            status, download_version = movies_downloader.download_torrent(series.lower(), season_number,
                                                                          episode_number, episode_download_directory)
            if download_version:
                subtitles_downloader.download_subtitles(series, season_number, episode_number,
                                                        download_version, episode_download_directory)
            if status == Status.no_connection:
                time.sleep(60)
        if status == Status.no_results:
            break
        elif status == Status.success:
            downloaded_episodes.append(episode_number)
    print "The following episodes: %s were downloaded successfully" % downloaded_episodes

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-sea', '--season_number')
    parser.add_argument('-ser', '--series')
    parser.add_argument('-e', '--episode_number')
    parser.add_argument('-d', '--download_directory')
    parser.add_argument('-n', '--hebrew_series_name')
    args = parser.parse_args()
    run(**args.__dict__)
