"""Local candidate generators feeding the project's actual RRF fusion function.

This is NOT the production Chroma embedding retriever. It uses BM25 and
character TF-IDF so every published fixture runs without models, secrets or GPUs.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from deep_research.memory.retrieval import fuse_records


def bm25_terms(text: str) -> list[str]:
    lowered = text.casefold()
    tokens = re.findall(r'[a-z][a-z0-9.-]*|\d+(?:[-/]\d+)*|[\u4e00-\u9fff]+', lowered)
    out: list[str] = []
    for token in tokens:
        if re.fullmatch(r'[\u4e00-\u9fff]+', token):
            out.extend(token[i:i+2] for i in range(len(token)-1))
        else:
            out.append(token)
    return out


def char_features(text: str) -> list[str]:
    text = re.sub(r'[^\w\u4e00-\u9fff]+', '', text.casefold())
    return [text[i:i+2] for i in range(len(text)-1)] + [text[i:i+3] for i in range(len(text)-2)]


class OfflineIndex:
    def __init__(self, records: list[dict]):
        if not records or len({record['id'] for record in records}) != len(records):
            raise ValueError('corpus must have unique records')
        self.records = records
        self.by_id = {x['id']:x for x in records}
        self.bm = [Counter(bm25_terms(x['title']+' '+x['content'])) for x in records]
        self.ch = [Counter(char_features(x['title']+' '+x['content'])) for x in records]
        self.df_bm = Counter(term for terms in self.bm for term in terms)
        self.df_ch = Counter(term for terms in self.ch for term in terms)
        self.avg_len = sum(sum(r.values()) for r in self.bm) / len(records)
        self.n = len(records)
        self._char_postings = defaultdict(list)
        for record, terms in zip(records, self.ch):
            norm2 = 0.0
            weighted = {}
            for term, count in terms.items():
                idf = math.log((self.n+1)/(1+self.df_ch[term])) + 1
                w = (1+math.log(count))*idf
                weighted[term] = w
                norm2 += w*w
            denom = math.sqrt(norm2) or 1.0
            for term, weight in weighted.items():
                self._char_postings[term].append((record['id'],weight/denom))

    def _scores_bm(self, query: str) -> dict[str,float]:
        terms = Counter(bm25_terms(query))
        scores = defaultdict(float)
        for record, doc in zip(self.records, self.bm):
            dl = sum(doc.values())
            for term, q_count in terms.items():
                tf = doc.get(term, 0)
                if not tf:
                    continue
                df = self.df_bm[term]
                idf = math.log(1.0 + (self.n-df+0.5)/(df+0.5))
                denom = tf + 1.2*(1-0.75+0.75*dl/self.avg_len)
                scores[record['id']] += idf * tf * 2.2/denom * min(q_count, 2)
        return scores

    def _scores_char(self, query: str) -> dict[str,float]:
        q = Counter(char_features(query))
        q_weights = {}
        for key, count in q.items():
            idf = math.log((self.n+1)/(1+self.df_ch[key])) + 1
            q_weights[key] = (1 + math.log(count)) * idf
        qnorm = math.sqrt(sum(x*x for x in q_weights.values())) or 1.0
        scores = defaultdict(float)
        for term, weight in q_weights.items():
            for ident, normal_weight in self._char_postings.get(term, ()):
                scores[ident] += weight * normal_weight / qnorm
        return scores

    def _scores_entity(self, query: str) -> dict[str,float]:
        folded = query.casefold()
        scores = defaultdict(float)
        for record in self.records:
            names = [record.get('entity_name', '')]+record.get('entity_aliases', [])
            if any(name and name.casefold() in folded for name in names):
                scores[record['id']] = 1.0
        return scores

    def _rank(self, scores: dict[str,float], n: int=60) -> list[dict]:
        ranks = sorted(scores, key=lambda k:(-scores[k],k))[:n]
        return [self._to_fusion_record(self.by_id[k]) for k in ranks]

    @staticmethod
    def _to_fusion_record(record: dict) -> dict:
        return {'id':record['id'], 'content':record['content'],
                'metadata':{'report_id':record['report_id'], 'published_at':record['published_at']}}

    def rank(self, query: str, variant: str, limit: int=10) -> list[str]:
        bm = self._rank(self._scores_bm(query))
        ch = self._rank(self._scores_char(query))
        entity = self._rank(self._scores_entity(query))
        if variant == 'bm25_local':
            return [x['id'] for x in bm[:limit]]
        if variant == 'char_tfidf_local':
            return [x['id'] for x in ch[:limit]]
        if variant == 'rrf_bm25_char':
            channels = [ch, bm]
        elif variant == 'rrf_bm25_char_entity':
            channels = [ch, bm, entity]
        else:
            raise ValueError(f'unknown offline variant {variant}')
        return [x['id'] for x in fuse_records(query, channels, limit=limit, per_report=2)]

VARIANTS = ('bm25_local','char_tfidf_local','rrf_bm25_char','rrf_bm25_char_entity')
