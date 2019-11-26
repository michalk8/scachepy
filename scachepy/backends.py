from abc import ABC, abstractmethod
from collections import Iterable
from pathlib import Path

import numpy as np
import os
import re
import pickle
import warnings


class Backend(ABC):

    def __init__(self, dirname, ext, *, cache):
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        self._dirname = Path(dirname)
        self._cache = cache
        self.ext = ext

    def _clear(self, verbose=1, typp='all', separator=None):
        files = [f for f in os.listdir(self.dir) if f.endswith(self.ext) and
                 os.path.isfile(os.path.join(self.dir, f))]
        if verbose > 0:
            print(f'Deleting {len(files)} files from `{typp}`.')

        for f in files:
            if verbose > 1:
                print(f'Deleting `{f}`.')
            os.remove(os.path.join(self.dir, f))

        if separator is not None:
            print(separator)
    
    @property
    def dir(self):
        return self._dirname

    @dir.setter
    def dir(self, value):
        if not isinstance(value, Path):
            value = Path(value)

        if self._cache._separate_dirs:
            self._dirname = self._cache.root_dir / value
        else:
            self._dirname = value
            self._cache._root_dir = value

        if not self._dirname.exists():
            os.makedirs(self._dirname)

    @abstractmethod
    def load(self, adata, fname, *args, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def save(self, adata, fname, attrs, keys, *args, keyhint=None, **kwargs):
        raise NotImplementedError

    def __repr__(self):
        return f"{self.__class__.__name__}(dir='{self.dir}', ext='{self.ext}')"


class PickleBackend(Backend):

    def load(self, adata, fname, *args, **kwargs):
        skip_if_not_found = kwargs.get('skip', False)
        verbose = kwargs.get('verbose', False)

        with open(os.path.join(self.dir, fname), 'rb') as fin:
            attrs_keys, vals = zip(*pickle.load(fin))

            for (attr, key), val in zip(attrs_keys, vals):
                if val is None:
                    msg = f'Cache contains empty value for attribute `{attr}`, key `{key}`. This could have happened when caching these values failed.'
                    if not skip_if_not_found:
                        raise RuntimeError(msg + ' Use `skip=True` to skip the aforementioned keys.')
                    warnings.warn(msg + ' Skipping.')
                    continue

                if key is None or isinstance(key, str):
                    key = (key, )

                if not hasattr(adata, attr):
                    if attr == 'obsm': shape = (adata.n_obs, )
                    elif attr == 'varm': shape = (adata.n_vars, )
                    else: raise AttributeError('Supported are only `.varm` and `.obsm` attributes.')

                    assert len(keys) == 1, 'Multiple keys not allowed in this case.'
                    setattr(adata, attr, np.empty(shape))

                if key[0] is not None:
                    at = getattr(adata, attr)
                    msg = [f'adata.{attr}']

                    for k in key[:-1]:
                        if k not in at.keys():
                            at[k] = dict()
                        at = at[k]
                        msg.append(f'[\'{k}\']')

                    if verbose and key[-1] in at.keys():
                        print(f'`{"".join(msg)}` already contains key: `\'{key[-1]}\'`.')

                    at[key[-1]] = val
                else:
                    if verbose and hasattr(adata, attr):
                        print(f'`adata.{attr}` already exists.')

                    setattr(adata, attr, val)

        return True

    def save(self, adata, fname, attrs, keys, *args,
             keyhint=None, watcher_keys={}, watchers={}, **kwargs):
        
        # value not found from _get_val
        sentinel = object()

        def _get_val(obj, keys, optional):
            try:
                if keys is None:
                    return obj

                if isinstance(keys, str) or not isinstance(keys, Iterable):
                    keys = (keys, )

                for k in keys:
                    obj = obj[k]

            except KeyError as e:
                if optional:
                    return sentinel

                msg = f'Unable to find keys `{", ".join(map(str, keys))}`.'
                if not skip_not_found:
                    raise RuntimeError(msg + ' Use `skip=True` to skip the aforementioned keys.') from e
                warnings.warn(msg + ' Skipping.')

                return sentinel

            return obj

        def _convert_key(attr, key, watcher_key, optional):
            watched_keys = watchers.get(watcher_key, {})

            if key is None or isinstance(key, str):
                return watched_keys.get(key, key), False

            if isinstance(key, re._pattern_type):
                km = {key.match(k).groups()[0]:k for k in getattr(adata, attr).keys() if key.match(k) is not None}
                res = set(km.keys()) & possible_vals

                if len(res) == 0:
                    # default value was not specified during the call
                    if len(km) == 0 and optional:
                        return sentinel, False

                    # found multiple matching keys
                    if len(km) != 1:
                        assert keyhint is not None, \
                                f'Found ambiguous matches for `{key}` in `adata.{attr}`: `{set(km.values())}`. ' \
                                'Try specifying `keyhint=\'...\'` to filter them out.'

                        # resolve by keyhint
                        filter_fn = (lambda v: keyhint.match(v) is not None) \
                                if isinstance(keyhint, re._pattern_type) else (lambda v: all(k in v for k in keyhint)) \
                                if isinstance(keyhint, (tuple, list)) else (lambda v: keyhint in v)
                        return tuple(v for v in km.values() if filter_fn(v)), True

                    # only 1 value
                    return tuple(km.values())[0], False

                if len(res) != 1:
                    mapped_key, all_failed = _map_watched_keys(watched_keys, res)

                    assert keyhint is not None, \
                                f'Found ambiguous matches for `{key}` in `adata.{attr}`: `{res}`. ' \
                                'Try specifying `keyhint=\'...\'` to filter them out.'

                    # resolve by keyhint
                    filter_fn = (lambda v: keyhint.match(v) is not None) \
                            if isinstance(keyhint, re._pattern_type) else (lambda v: all(k in v for k in keyhint)) \
                            if isinstance(keyhint, (tuple, list)) else (lambda v: keyhint in v)

                    return tuple(v for v in km.values() if filter_fn(v)), True

                # only 1 value
                return km[res.pop()], False

            assert isinstance(key, Iterable)
            key, _ = _map_watched_keys(watcher_key, key),

            return key, False

        def _map_watched_keys(watched_keys, keys):
            if not isinstance(keys, (tuple, list)):
                keys = (keys, )

            res = tuple(watched_keys[k] for k in keys if k in watched_keys)
            return res, len(res) > 0

        def _get_data(adata, attr, key, opt):
            value = _get_val(getattr(adata, attr), key, opt)
            # value not found - either skipping or optional
            if value is sentinel:
                return

            if key is None or isinstance(key, str):
                key = (key, )

            return (attr, key), value


        verbose = kwargs.get('verbose', False)
        skip_not_found = kwargs.get('skip', False)
        possible_vals = kwargs.get('possible_vals', {})
        is_optional = kwargs.get('is_optional', [False] * len(attrs))

        data = []
        # watcher_key is just like attr, but without _opt _cache stripped
        for attr, key, watcher_key, opt in zip(attrs, keys, watcher_keys, is_optional):
            if not hasattr(adata, attr):
                if opt:
                    continue
                raise AttributeError(f'`adata` object has no attribute `{attr}` and '
                                      'it was not specified as optional.')

            key, check_for_mul_keys = _convert_key(attr, key, watcher_key, opt)
            if key is sentinel:  # optional key not found
                continue
            elif check_for_mul_keys:  # keyhint returned multiple
                for k in key:
                    d = _get_data(adata, attr, k, opt)
                    if d is not None:
                        data.append(d)
            else:
                d = _get_data(adata, attr, key, opt)
                if d is not None:
                    data.append(d)

        with open(os.path.join(self.dir, fname), 'wb') as fout:
            pickle.dump(data, fout)

        return True
