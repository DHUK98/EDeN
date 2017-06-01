#!/usr/bin/env python
"""Provides scikit interface."""

import numpy as np
from eden.graph import vectorize
from eden.util import timeit
import random
from toolz.sandbox.core import unzip
from collections import Counter
from toolz.curried import first, second, groupby
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.linear_model import SGDClassifier
import dask_searchcv as dcv
from sklearn.model_selection import learning_curve
from sklearn.model_selection import ShuffleSplit
from sklearn.metrics import classification_report
from sklearn.model_selection import cross_val_score
from sklearn.metrics import average_precision_score
from sklearn.metrics import accuracy_score
from sklearn.metrics import roc_auc_score
from sklearn.metrics import make_scorer
import pylab as plt
from eden.display import plot_confusion_matrices
from eden.display import plot_aucs
from sklearn.cluster import MiniBatchKMeans
from dask.diagnostics import Profiler, ResourceProfiler, CacheProfiler
from dask.diagnostics import visualize
import multiprocessing as mp
import logging

logger = logging.getLogger()


def paired_shuffle(iterable1, iterable2):
    """paired_shuffle."""
    i1i2 = zip(iterable1, iterable2)
    random.shuffle(i1i2)
    i1, i2 = unzip(i1i2)
    return list(i1), list(i2)


@timeit
def subsample(graphs, targets, subsample_size=100):
    """subsample."""
    tg = zip(targets, graphs)
    num_classes = len(set(targets))
    class_graphs = groupby(lambda x: first(x), tg)
    subgraphs = []
    subtargets = []
    for y in class_graphs:
        class_subgraphs = class_graphs[y][:subsample_size / num_classes]
        class_subgraphs = [second(x) for x in class_subgraphs]
        subgraphs += class_subgraphs
        subtargets += [y] * len(class_subgraphs)
    subgraphs, subtargets = paired_shuffle(subgraphs, subtargets)
    return list(subgraphs), list(subtargets)


@timeit
def balance(graphs, targets, estimator, ratio=2):
    """balance."""
    class_counts = Counter(targets)
    majority_class = None
    max_count = 0
    minority_class = None
    min_count = 1e6
    for class_key in class_counts:
        if max_count < class_counts[class_key]:
            majority_class = class_key
            max_count = class_counts[class_key]
        if min_count > class_counts[class_key]:
            minority_class = class_key
            min_count = class_counts[class_key]

    desired_size = int(min_count * ratio)

    tg = zip(targets, graphs)
    class_graphs = groupby(lambda x: first(x), tg)
    maj_graphs = [second(x) for x in class_graphs[majority_class]]
    min_graphs = [second(x) for x in class_graphs[minority_class]]

    if estimator:
        # select only the instances in the majority class that
        # have a small margin
        preds = estimator.decision_function(maj_graphs)
    else:
        # select at random
        preds = [random.random() for i in range(len(maj_graphs))]
    preds = [abs(pred) for pred in preds]
    pred_graphs = sorted(zip(preds, maj_graphs))[:desired_size]
    maj_graphs = [g for p, g in pred_graphs]

    bal_graphs = min_graphs + maj_graphs
    bal_pos = [minority_class] * len(min_graphs)
    bal_neg = [majority_class] * len(maj_graphs)
    bal_targets = bal_pos + bal_neg

    return paired_shuffle(bal_graphs, bal_targets)


def make_train_test_sets(pos_graphs, neg_graphs,
                         test_proportion=.3, random_state=2):
    """make_train_test_sets."""
    random.seed(random_state)
    random.shuffle(pos_graphs)
    random.shuffle(neg_graphs)
    pos_dim = len(pos_graphs)
    neg_dim = len(neg_graphs)
    tr_pos_graphs = pos_graphs[:-int(pos_dim * test_proportion)]
    te_pos_graphs = pos_graphs[-int(pos_dim * test_proportion):]
    tr_neg_graphs = neg_graphs[:-int(neg_dim * test_proportion)]
    te_neg_graphs = neg_graphs[-int(neg_dim * test_proportion):]
    tr_graphs = tr_pos_graphs + tr_neg_graphs
    te_graphs = te_pos_graphs + te_neg_graphs
    tr_targets = [1] * len(tr_pos_graphs) + [0] * len(tr_neg_graphs)
    te_targets = [1] * len(te_pos_graphs) + [0] * len(te_neg_graphs)
    tr_graphs, tr_targets = paired_shuffle(tr_graphs, tr_targets)
    te_graphs, te_targets = paired_shuffle(te_graphs, te_targets)
    return (tr_graphs, np.array(tr_targets)), (te_graphs, np.array(te_targets))


class EdenEstimator(BaseEstimator, ClassifierMixin):
    """Build an estimator for graphs."""

    def __init__(self, r=1, d=1, n_jobs=-1, discrete=True,
                 balance=False, subsample_size=200, ratio=2):
        """construct."""
        self.set_params(r, d, n_jobs, discrete, balance, subsample_size, ratio)

    def set_params(self, r=2, d=2, n_jobs=-1, discrete=True,
                   balance=False, subsample_size=200, ratio=2):
        """setter."""
        self.r = r
        self.d = d
        self.n_jobs = n_jobs
        self.discrete = discrete
        self.balance = balance
        self.subsample_size = subsample_size
        self.ratio = ratio
        self.model = SGDClassifier(
            average=True, class_weight='balanced', shuffle=True, n_jobs=n_jobs)
        return self

    def transform(self, graphs):
        """transform."""
        x = vectorize(
            graphs, r=self.r, d=self.d,
            discrete=self.discrete, n_jobs=self.n_jobs)
        return x

    @timeit
    def fit(self, graphs, targets, randomize=True):
        """fit."""
        if self.balance:
            if randomize:
                bal_graphs, bal_targets = balance(
                    graphs, targets, None, ratio=self.ratio)
            else:
                samp_graphs, samp_targets = subsample(
                    graphs, targets, subsample_size=self.subsample_size)
                x = self.transform(samp_graphs)
                self.model.fit(x, samp_targets)
                bal_graphs, bal_targets = balance(
                    graphs, targets, self, ratio=self.ratio)
            size = len(bal_targets)
            logger.debug('Dataset size=%d' % (size))
            x = self.transform(bal_graphs)
            self.model = self.model.fit(x, bal_targets)
        else:
            x = self.transform(graphs)
            self.model = self.model.fit(x, targets)
        return self

    @timeit
    def predict(self, graphs):
        """predict."""
        x = self.transform(graphs)
        preds = self.model.predict(x)
        return preds

    @timeit
    def decision_function(self, graphs):
        """decision_function."""
        x = self.transform(graphs)
        preds = self.model.decision_function(x)
        return preds

    @timeit
    def cross_val_score(self, graphs, target,
                        scoring='roc_auc', cv=5):
        """cross_val_score."""
        x = self.transform(graphs)
        scores = cross_val_score(
            self.model, x, target, cv=cv,
            scoring=scoring, n_jobs=self.n_jobs)
        return scores

    @timeit
    def model_selection(self, graphs, targets, subsample_size=None):
        """model_selection."""
        return self._model_selection(
            graphs, targets, None, subsample_size, mode='grid')

    @timeit
    def model_selection_rand(self, graphs, targets,
                             n_iter=30, subsample_size=None):
        """model_selection_randomized."""
        param_distr = {"r": list(range(1, 5)), "d": list(range(0, 6))}
        if subsample_size:
            graphs, targets = subsample(
                graphs, targets, subsample_size=subsample_size)

        pool = mp.Pool()
        scores = pool.map(_eval, [(graphs, targets, param_distr)] * n_iter)
        pool.close()
        pool.join()

        best_params = max(scores)[1]
        logger.debug("Best parameters:\n%s" % (best_params))
        self = EdenEstimator(**best_params)
        return self

    def _model_selection(self, graphs, targets, n_iter=30,
                         subsample_size=None, mode='randomized'):
        with Profiler() as prof, ResourceProfiler(dt=0.25) as rprof, CacheProfiler() as cprof:
            param_distr = {"r": list(range(1, 4)), "d": list(range(0, 5))}
            if mode == 'randomized':
                search = dcv.RandomizedSearchCV(
                    self, param_distr, cv=3, n_iter=n_iter)
            else:
                search = dcv.GridSearchCV(
                    self, param_distr, cv=3)
            if subsample_size:
                graphs, targets = subsample(
                    graphs, targets, subsample_size=subsample_size)
            search = search.fit(graphs, targets)
            logger.debug("Best parameters:\n%s" % (search.best_params_))
            self = search.best_estimator_
            self.r = search.best_params_['r']
            self.d = search.best_params_['d']
            visualize([prof, rprof, cprof])
        return self

    @timeit
    def learning_curve(self, graphs, targets, cv=5, n_steps=10):
        """learning_curve."""
        x = self.transform(graphs)
        train_sizes = np.linspace(0.1, 1.0, n_steps)
        scoring = 'roc_auc'
        train_sizes, train_scores, test_scores = learning_curve(
            self.model, x, targets,
            cv=cv, train_sizes=train_sizes,
            scoring=scoring, n_jobs=self.n_jobs)
        return train_sizes, train_scores, test_scores

    def bias_variance_decomposition(self, graphs, n_bootstraps=10):
        """bias_variance_decomposition."""
        pass


def _sample_params(param_distr):
    params = dict()
    for key in param_distr:
        params[key] = random.choice(param_distr[key])
    return params


def _eval_params(graphs, targets, param_distr):
    # sample parameters
    params = _sample_params(param_distr)
    # create model with those parameters
    est = EdenEstimator(n_jobs=1, **params)
    # run a cross_val_score
    scores = est.cross_val_score(graphs, targets)
    # return average
    return np.mean(scores), params


def _eval(data):
    return _eval_params(*data)


@timeit
def process_vec_info(g, n_clusters=8, cv=3):
    """process_vec_info."""
    # extract node vec information and make np data matrix
    data_matrix = np.array([g.node[u]['vec'] for u in g.nodes()])
    # cluster with kmeans
    clu = MiniBatchKMeans(n_clusters=n_clusters, n_init=10)
    clu.fit(data_matrix)
    preds = clu.predict(data_matrix)
    vecs = clu.transform(data_matrix)
    vecs = 1 / (1 + vecs)
    # replace node information
    graph = g.copy()
    for u in graph.nodes():
        graph.node[u]['label'] = str(preds[u])
        graph.node[u]['vec'] = list(vecs[u])
    return graph


@timeit
def estimate_predictive_performance(x_y,
                                    estimator=None,
                                    n_splits=10,
                                    random_state=1):
    """estimate_predictive_performance."""
    x, y = x_y
    cv = ShuffleSplit(n_splits=n_splits,
                      test_size=0.3,
                      random_state=random_state)
    scoring = make_scorer(average_precision_score)
    scores = cross_val_score(estimator, x, y, cv=cv, scoring=scoring)
    return scores


def output_avg_and_std(iterable):
    """output_avg_and_std."""
    print('score: %.2f +-%.2f' % (np.mean(iterable), np.std(iterable)))
    return iterable


@timeit
def perf(y_true, y_pred, y_score):
    """perf."""
    print 'Accuracy: %.2f' % accuracy_score(y_true, y_pred)
    print ' AUC ROC: %.2f' % roc_auc_score(y_true, y_score)
    print '  AUC AP: %.2f' % average_precision_score(y_true, y_score)
    print
    print 'Classification Report:'
    print classification_report(y_true, y_pred)
    print
    plot_confusion_matrices(y_true, y_pred, size=int(len(set(y_true)) * 2.5))
    print
    plot_aucs(y_true, y_score, size=10)


def compute_stats(scores):
    """compute_stats."""
    median = np.percentile(scores, 50, axis=1)
    low = np.percentile(scores, 25, axis=1)
    high = np.percentile(scores, 75, axis=1)
    low10 = np.percentile(scores, 10, axis=1)
    high90 = np.percentile(scores, 90, axis=1)
    return median, low, high, low10, high90


def plot_stats(x=None, y=None, label=None, color='navy'):
    """plot_stats."""
    y = np.array(y)
    y0 = y[0]
    y1 = y[1]
    y2 = y[2]
    y3 = y[3]
    y4 = y[4]
    plt.fill_between(x, y3, y4, color=color, alpha=0.08)
    plt.fill_between(x, y1, y2, color=color, alpha=0.08)
    plt.plot(x, y0, '-', lw=2, color=color, label=label)
    plt.plot(x, y0,
             linestyle='None',
             markerfacecolor='white',
             markeredgecolor=color,
             marker='o',
             markeredgewidth=2,
             markersize=8)


def plot_learning_curve(train_sizes, train_scores, test_scores):
    """plot_learning_curve."""
    plt.figure(figsize=(15, 5))
    plt.title('Learning Curve')
    plt.xlabel("Training examples")
    plt.ylabel("AUC ROC")
    tr_ys = compute_stats(train_scores)
    te_ys = compute_stats(test_scores)
    plot_stats(train_sizes, tr_ys,
               label='Training score',
               color='navy')
    plot_stats(train_sizes, te_ys,
               label='Cross-validation score',
               color='orange')
    plt.grid(linestyle=":")
    plt.legend(loc="best")
    plt.show()