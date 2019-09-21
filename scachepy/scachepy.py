from .backends import PickleBackend
from .utils import Module, FunctionWrapper, wrap_as_adata, SC_TMP_PLOT_KEY

from functools import wraps
from collections import Iterable, namedtuple
from inspect import signature
from matplotlib.backends.backend_tkagg import FigureCanvasAgg
from PIL import Image

import scvelo as scv
import scanpy as sc
import anndata

import numpy as np
import matplotlib as mpl
import os
import re
import pickle
import traceback
import warnings


class Cache:

    _backends = dict(pickle=PickleBackend)
    _extensions = dict(pickle='.pickle')

    def __init__(self, cache_dir, backend='pickle',
                 ext=None, make_dir=True):
        '''
        Params
        --------
        cache_dir: str
            path to directory where to save the files
        backend: str, optional (default: `'pickle'`)
            which backend to use
        ext: str, optional (default: `None`)
            file extensions, defaults to '.pickle' for
            'pickle' backend; defaults to '.scdata' if non applicable
        make_dir: bool, optional (default: `True`)
            make the `cache_dir` if it does not exist
        '''

        self._backend = self._backends.get(backend, None)
        if self._backend is None:
            raise ValueError(f'Unknown backend type: `{backend}`. Supported backends are: `{", ".join(self._backends.keys())}`.')

        cache_dir = os.path.expanduser(cache_dir)
        cache_dir = os.path.abspath(cache_dir)

        self._backend = self._backend(cache_dir, make_dir=make_dir)
        self._ext = ext if ext is not None else self._extensions.get(backend, '.scdata')

        self._init_pp()
        self._init_tl()
        self._init_pl()

    def _init_pp(self):
        functions = {
            # TODO: not ideal - the FunctionWrapper requires the function to be specified
            # we also must wrap the last function as opposed to the function returned by self.cache
            'pcarr': FunctionWrapper(wrap_as_adata(self.cache(dict(obsm='X_pca'),
                                                              default_fname='pca_arr',
                                                              default_fn=sc.pp.pca,
                                                              wrap=False),
                                                   ret_attr=dict(obsm='X_pca')),
                                 sc.pp.pca),
            'expression': self.cache(dict(X=None), default_fname='expression'),
            'moments': self.cache(dict(uns='pca',
                                       uns_cache1='neighbors',
                                       obsm='X_pca',
                                       varm='PCs',
                                       layers='Ms',
                                       layers_cache1='Mu'),
                                  default_fn=scv.pp.moments,
                                  default_fname='moments'),
             'pca': self.cache(dict(obsm='X_pca',
                                    varm='PCs',
                                    uns=['pca', 'variance_ratio'],
                                    uns_cache1=['pca', 'variance']),
                               default_fname='pca',
                               default_fn=sc.pp.pca),
             'neighbors': self.cache(dict(uns='neighbors'),
                                     default_fname='neighs',
                                     default_fn=sc.pp.neighbors)
        }
        self.pp = Module('pp', **functions)

    def _init_tl(self):
        functions = {
            'louvain': self.cache(dict(obs='louvain'),
                                  default_fname='louvain',
                                  default_fn=sc.tl.louvain),
            'tsne': self.cache(dict(obsm='X_tsne'),
                               default_fname='tsne',
                               default_fn=sc.tl.tsne),
            'umap': self.cache(dict(obsm='X_umap'),
                               default_fname='umap',
                               default_fn=sc.tl.umap),
            'diffmap': self.cache(dict(obsm='X_diffmap',
                                       uns='diffmap_evals',
                                       uns_cache1='iroot'),
                                  default_fname='diffmap',
                                  default_fn=sc.tl.diffmap),
            'paga': self.cache(dict(uns='paga'),
                               default_fn=sc.tl.paga,
                               default_fname='paga'),
            'velocity': self.cache(dict(var='velocity_gamma',
                                        var_cache1='velocity_r2',
                                        var_cache2='velocity_genes',
                                        layers='velocity'),
                                   default_fn=scv.tl.velocity,
                                   default_fname='velo'),
            'velocity_graph': self.cache(dict(uns=re.compile(r'(.+)_graph$'),
                                              uns_cache1=re.compile('(.+)_graph_neg$')),
                                         default_fn=scv.tl.velocity_graph,
                                         default_fname='velo_graph'),
            'velocity_embedding': self.cache(dict(obsm=re.compile(r'^velocity_(.+)$')),
                                             default_fn=scv.tl.velocity_embedding,
                                             default_fname='velo_emb'),
            'draw_graph': self.cache(dict(obsm=re.compile(r'^X_draw_graph_(.+)$'),
                                          uns='draw_graph'),
                                     default_fn=sc.tl.draw_graph,
                                     default_fname='draw_graph')
        }
        self.tl = Module('tl', **functions)

    # TODO: maybe let scanpy write it to disk and read it from there?
    def _init_pl(self):

        def wrap(fn):

            @wraps(fn)
            def wrapper(adata, *args, **kwargs):
                if SC_TMP_PLOT_KEY in adata.uns:
                    return

                return_fig = kwargs.pop('return_fig', None)
                fig = fn(adata, *args, **kwargs, return_fig=True)

                adata.uns[SC_TMP_PLOT_KEY] = fig2data(fig)

            def fig2data(fig):
                canvas = FigureCanvasAgg(fig)
                canvas.draw()
                s, (width, height) = canvas.print_to_buffer()

                return np.fromstring(s, np.uint8).reshape((height, width, 4))

            return wrapper

        functions = {fn.__name__:self.cache(dict(uns=SC_TMP_PLOT_KEY),
                                            default_fname=f'{fn.__name__}_plot',
                                            default_fn=wrap(fn),
                                            is_plot=True)
                                            
        for fn in filter(lambda fn: np.in1d(['return_fig'],  # only this works (wanted to  have with 'show')
                                            list(signature(fn).parameters.keys())).all(),
                         filter(callable, map(lambda name: getattr(sc.pl, name), dir(sc.pl))))}
                               
        self.pl = Module('pl', **functions)

    def __repr__(self):
        return f"{self.__class__.__name__}(backend={self.backend}, ext='{self._ext}')"

    @property
    def backend(self):
        return self._backend

    @backend.setter
    def backend(self, _):
        raise RuntimeError('Setting backend is disallowed.') 

    def _create_cache_fn(self, *args, default_fname=None):

        def wrapper(adata, fname=None, recache=False, verbose=True, skip=False, *args, **kwargs):
            try:
                if fname is None:
                    fname = default_fname
                if not fname.endswith(self._ext):
                    fname += self._ext

                if recache:
                    possible_vals = set(args) | set(kwargs.values())
                    return self.backend.save(adata, fname, attrs, keys,
                                             skip=skip,
                                             possible_vals=possible_vals, verbose=verbose)

                if (self.backend.dir / fname).is_file():
                    if verbose:
                        print(f'Loading data from: `{fname}`.')

                    return self.backend.load(adata, fname, verbose=verbose, skip=skip)

                return False

            except Exception as e:
                if not isinstance(e, FileNotFoundError):
                    if recache:
                        print(traceback.format_exc())
                else:
                    print(f'No cache found in `{self.backend.dir / fname}`.')

                return False

        # if you're here because of the doc, you're here correctly
        if len(args) == 1:
            collection = args[0]
            if isinstance(collection, dict):
                attrs = tuple(collection.keys())
                keys = tuple(collection.values())
            elif isinstance(collection, Iterable) and len(next(iter(collection))) == 2:
                attrs, keys = tuple(zip(*collection))
            else:
                raise RuntimeError('Unable to decode the args of length 1.')
        elif len(args) == 2:
            attrs, keys = args
            if isinstance(attrs, str):
                attrs = (attrs, )
            if isinstance(keys, str):
                keys = (keys, )
        else:
            raise RuntimeError('Expected the args to be of length 1 or 2.')

        # strip the postfix
        pat = re.compile(r'_cache\d+$')
        attrs = tuple(pat.sub('', a) for a in attrs)

        return wrapper


    def cache(self, *args, wrap=True, **kwargs):
        '''
        Create a caching function.

        Params
        --------
        args: dict(str, Union[str, Iterable[Union[str, re._pattern_type]]])
            attributes are supplied as dictionary keys and
            values as dictionary values (need not be an Iterable)
            for caching multiple attributes of the same name,
            append to them postfixes of the following kind: `_cache1, _cache2, ...`
            there are also other ways of specifying this, please
            refer the source code of `_create_cache_fn`
        wrap: bool, optional (default: `True`)
            whether to wrap in a pretty printing wrapper
        default_fname: str
            default filename where to save the pickled data
        default_fn: callable, optional (default: `None`)
            function to call before caching the values

        Returns
        --------
        a caching function accepting as the first argument either
        anndata.AnnData object or a callable and anndata.AnnData
        object as the second argument
        '''

        def wrapper(*args, **kwargs):
            fname = kwargs.pop('fname', None)
            force = kwargs.pop('force', False)
            verbose = kwargs.pop('verbose', True)
            call = kwargs.pop('call', True)  # if we do not wish to call the callback
            skip = kwargs.pop('skip', False)
            # leave it in kwargs
            copy = kwargs.get('copy', False) and not is_plot

            assert fname is not None or def_fname is not None, f'No filename or default specified.'

            callback = None
            if len(args) > 1:
                callback, *args = args

            is_raw = False
            if len(args) > 0 and isinstance(args[0], (anndata.AnnData, anndata.Raw)):
                if isinstance(args[0], anndata.Raw):
                    args = (args[0]._adata, *args[1:])
                    is_raw = True
                adata = args[0]
            elif 'adata' in kwargs:
                if isinstance(kwargs['adata'], anndata.Raw):
                    kwargs['adata'] = kwargs['adata']._adata
                    is_raw = True
                adata = kwargs['adata']
            else:
                raise ValueError(f'Unable to locate adata object in args or kwargs.')

            # at this point, it's impossible for adata to be of type anndata.Raw
            # but the message should tell it's possible for it to be an input
            assert isinstance(adata, (anndata.AnnData, )), f'Expected `{adata}` to be of type `anndata.AnnData` or `anndata.Raw`, found `{type(adata)}`.'

            # forcing always forces the callback
            if (call or force) and callback is None:
                if default_fn is None:
                    raise RuntimeError('No callback specified and default is None; specify it as a 1st argument. ')
                callback = default_fn
                assert callable(callback), f'`{callback}` is not callable.'

            if force:
                if verbose:
                    print('Computing values (forced).')
                if not call:
                    warnings.warn('Specifying `call=False` and `force=True` still forces the computation.')
                res = callback(*args, **kwargs)
                ret = cache_fn(res if copy else adata, fname, True, verbose, skip, *args, **kwargs)
                assert ret, 'Caching failed, horribly.'

                if is_plot:
                    del adata.uns[SC_TMP_PLOT_KEY]
                    return

                return anndata.Raw(res) if is_raw and res is not None else res

            # when loading to cache and copy is true, modify the copy
            if copy:
                adata = adata.copy()

            # we need to pass the *args and **kwargs in order to
            # get the right field when using regexes
            if not cache_fn(adata, fname, False, verbose, skip, *args, **kwargs):
                if verbose:
                    f = fname if fname is not None else def_fname
                    print(f'No cache found in `{str(f) + self._ext}`, ' + ('computing values.' if call else 'searching for values.'))
                res = callback(*args, **kwargs) if call else adata if copy else None
                ret = cache_fn(res if copy else adata, fname, True, False, skip, *args, **kwargs)
                assert ret, 'Caching failed, horribly.'

                if is_plot:
                    del adata.uns[SC_TMP_PLOT_KEY]
                    return

                return anndata.Raw(res) if is_raw and res is not None else res

            if is_plot:
                data = adata.uns[SC_TMP_PLOT_KEY].copy()
                del adata.uns[SC_TMP_PLOT_KEY]
                return Image.fromarray(data)

            # if cache was found and not modifying inplace
            if not copy:
                return None

            if is_raw:
                return anndata.Raw(adata)

            return adata

        def_fname = kwargs.get('default_fname', None)  # keep in in kwargs
        default_fn = kwargs.pop('default_fn', lambda *_x, **_y: None)
        is_plot = kwargs.pop('is_plot', False)
        cache_fn = self._create_cache_fn(*args, **kwargs)

        return FunctionWrapper(wrapper, default_fn) if wrap else wrapper 
