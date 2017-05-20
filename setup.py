from setuptools import setup

setup(name='torentDD',
      version='0.1',
      description='series downloader',
      url='http://github.com/orikalinski/torentDD',
      author='Ori Kalinski',
      author_email='orikalinski@gmail.com',
      license='',
      packages=['torrentDD'],
      install_requires=['textblob', 'dryscrape', 'requests', 'transmissionrpc', 
                        'bs4', 'fake_useragent', 'Levenshtein'],
      zip_safe=False)
