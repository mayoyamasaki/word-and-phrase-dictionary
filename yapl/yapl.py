import argparse
import bz2
from collections import Counter, defaultdict
import gzip
import glob
import hashlib
from html.parser import HTMLParser
from itertools import chain
import math
import os
import re
import subprocess
import urllib.request

from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize

from models import PhraseLexiconModel


def maybe_download(url, expected_hash):
    """Download a file from url if not present, and make sure it's sha1 hash. """
    try:
        filename = url.split('/')[-1]
    except:
        raise Exception('Failed to extract filename from url.')

    if not os.path.exists(filename):
        print('Downlod', url, '...')
        filename, _ = urllib.request.urlretrieve(url, filename)
        print('Downloded.')

    sha1 = hashlib.sha1()
    with open(filename, 'rb') as f:
        for chunk in iter(lambda: f.read(2048 * sha1.block_size), b''):
            sha1.update(chunk)
    checksum = sha1.hexdigest()
    if checksum == expected_hash:
        print('Found and verified', filename)
    else:
        print(checksum)
        raise Exception('Failed to verify ' + filename + '. Can you check url.')
    return filename


def insert_pagetitles_to_lexicon(filename, lexicon):
    """Insert enwiai pagetitles into sqlite3 via lexicon model"""
    if type(lexicon) != PhraseLexiconModel:
        raise Exception('Falied to access db.')

    def isnt_ignore(phrase):
        # /_\(.*\)$/
        if phrase[-1] == ')' and '_(' in phrase: return False
        # /^[a-zA-z]$/
        chars = list('abcdefghijklmnopqrstuvwxyz')
        if len(phrase) == 1 and phrase in chars: return False
        # /^[0-9|!-\/:-@\[-`\{-~]*$/
        chars = set('0123456789!-/:-@[-`{~')
        if len(set(phrase).difference(chars)) == 0: return False
        # /(disambiguation)/
        if '(disambiguation)' in phrase: return False
        # /^Lists_of/
        if 'Lists_of' == phrase[:8]: return False
        return True

    def sanitize(phrase):
        return phrase.lstrip('_').rstrip('_').replace('_', ' ')

    with gzip.open(filename, 'rt', encoding='utf-8') as f:
        _ = f.readline() # pass sql table name.
        striped_phrases = map(lambda row: row.rstrip('\n').lower(), f)
        ignored_phrases = filter(isnt_ignore, striped_phrases)
        sanitized_phrases = map(sanitize, ignored_phrases)
        phrases = map(lambda x: (x, ), sanitized_phrases)
        return lexicon.insert_phrases(phrases)


def insert_articles_to_lexicon(articles_filename, extracted_dir, lexicon):
    """get phrases from wikimedia articles

    PMI based phrases are counted by bigrams using Lossy Counting
    """
    if type(lexicon) != PhraseLexiconModel:
        raise Exception('Falied to access db.')

    # extract text from xml using wikiextractor
    cmd_to_extract_text = [
        'python3',
        './yapl/wikiextractor/WikiExtractor.py',
        articles_filename,
        '-o', extracted_dir,
        '-c',
        '-q'
    ]
    if not os.path.exists(extracted_dir):
        print('Extracting text from wiki xml ...')
        subprocess.call(cmd_to_extract_text)
    else:
        print('Found extracted text')

    print('Search phrase candidates ...')

    class BigramCounter():
        """Bigram Counter using Lossy Counting"""
        def __init__(self, delta, stopwords=[]):
            self.n = 0
            self.delta = delta
            self.bigrams = defaultdict(dict)
            self.bucket_ids = defaultdict(dict)
            self.current_bucket_id = 1
            self.stopwords = stopwords

        def add(self, t1, t2):
            self.n += 1
            if t1 not in self.stopwords and t2 not in self.stopwords:
                if t2 in self.bigrams[t1]:
                    self.bigrams[t1][t2] += 1
                else:
                    self.bigrams[t1][t2] = 1
                    self.bucket_ids[t1][t2] = self.current_bucket_id - 1

            if self.is_boundary_of_bucket():
                self.move_next_bucket()

        def is_boundary_of_bucket(self):
            return self.n % int(1 / self.delta) == 0

        def move_next_bucket(self):
            self.current_bucket_id += 1
            self.weed_out_bigrams()

        def weed_out_bigrams(self):
            deleted_bigram_queue = []
            for t1, subtree in self.bigrams.items():
                for t2, count in subtree.items():
                    if count <= self.current_bucket_id - self.bucket_ids[t1][t2]:
                        deleted_bigram_queue.append((t1, t2))
            for t1, t2 in deleted_bigram_queue:
                del self.bigrams[t1][t2]
                del self.bucket_ids[t1][t2]

    mystopwords = ',.()[]{}:;\'"+=_-^&*%$#@!~`|\\<>?/'
    sw = stopwords.words("english") + list(mystopwords)
    phrases = []
    threshold = 1000
    unigrams = Counter()
    counter = BigramCounter(5e-3, sw)
    for articlefile in  glob.glob(extracted_dir + '/*/*'):
        with bz2.open(articlefile, 'rt', encoding='utf8') as f:
            txt = f.readlines()[1:-1] # pass <doc *> and </doc> tags.
        tokens = list(map(lambda t: t.lower(),
                          chain.from_iterable(map(word_tokenize, txt))))
        unigrams += Counter(tokens)
        for t1, t2 in zip(tokens, tokens[1:]):
            counter.add(t1, t2)

    bigrams = counter.bigrams
    phrase_candidates = []
    count_all = sum(unigrams.values())
    for token_y, subtree in bigrams.items():
        count_all_given_y = sum(subtree.values())
        for token_x, count_x_given_y in subtree.items():
            # calc pmi = log (p(x|y) / p(x))
            p_x_given_y = count_x_given_y / count_all_given_y
            p_x = unigrams[token_x] / count_all
            pmi = math.log(p_x_given_y/p_x)
            if pmi >= threshold:
                phrases.append(token_y + ' ' + token_x)
    return lexicon.insert_phrases(map(lambda x: (x, ), phrases))


def main():
    parser = argparse.ArgumentParser(description='A Script for Create Phrae Lexicon Databse')
    parser.add_argument('--db-path',
                action='store',
                type=str,
                help='sqlite3 databese path.',
            )
    parser.add_argument('--wiki-titles-url',
                action='store',
                type=str,
                help='wikimedia page titles url for downloading.',
            )
    parser.add_argument('--wiki-titles-hash',
                action='store',
                type=str,
                help='wikimedia page titles sha1 hash for validation.',
            )

    parser.add_argument('--wiki-articles-url',
                action='store',
                type=str,
                help='wikimedia pages articles url for downloading.',
            )
    parser.add_argument('--wiki-articles-hash',
                action='store',
                type=str,
                help='wikimedia page articles sha1 hash for validation.',
            )
    parser.add_argument('--wiki-extracted-dir',
                action='store',
                type=str,
                help='directory path of extracted xml using wikiextractor',
            )
    args = parser.parse_args()

    lexicon = PhraseLexiconModel(args.db_path)

    titles_filename = maybe_download(args.wiki_titles_url, args.wiki_titles_hash)
    print('start insertng enwiki pagetitles...')
    total_cnt = insert_pagetitles_to_lexicon(titles_filename, lexicon)
    print('inserted {} page titles'.format(total_cnt))

    articles_filename = maybe_download(args.wiki_articles_url, args.wiki_articles_hash)
    print('start making phrases from articles...')
    total_cnt = insert_articles_to_lexicon(articles_filename, args.wiki_extracted_dir, lexicon)
    print('inserted {} phrases extracted articles'.format(total_cnt))

    print('done!')


if __name__ == '__main__':
    main()
