from .modules import PpModule, TlModule, PlModule
from .backends import PickleBackend
from .utils import *

import os
import warnings


class Cache:

    _backends = dict(pickle=PickleBackend)
    _extensions = dict(pickle='.pickle')

    def __init__(self, root_dir, backend='pickle',
                 ext=None, separate_dirs=False):
        '''
        Params
        --------
        root_dir: Str
            path to directory where to save the files
        backend: Str, optional (default: `'pickle'`)
            which backend to use
        ext: Str, optional (default: `None`)
            file extensions, defaults to '.pickle' for
            'pickle' backend; defaults to '.scdata' if non applicable
        seperate_dirs: Bool, optional (default: `True`)
            whether to create 'pp', 'tl' and 'pl' directories
            under the `root_dir`
        '''

        self._separate_dirs = separate_dirs

        backend_type = self._backends.get(backend, None)
        if backend_type is None:
            raise ValueError(f'Unknown backend type: `{backend_type}`. Supported backends are: `{", ".join(self._backends.keys())}`.')

        self._root_dir = os.path.expanduser(root_dir)
        self._root_dir = os.path.abspath(self._root_dir)
        self._ext = ext if ext is not None else self._extensions.get(backend, '.scdata')

        if self._separate_dirs:
            for where, Mod in zip(['pp', 'tl', 'pl'],
                                 [PpModule, TlModule, PlModule]):
                setattr(self, where, Mod(backend, dirname=os.path.join(self._root_dir, where), ext=self._ext))
        else:
            warnings.warn('`separate_dirs` option is `False`. This option will be removed in future release and cache will always create separate subdirectories.')
            # shared backend
            self._backend = backend_type(root_dir, self._ext)
            self.pp = PpModule(self._backend)
            self.tl = TlModule(self._backend)
            self.pl = PlModule(self._backend)

    @property
    def root_dir(self):
        return self._root_dir

    @root_dir.setter
    def root_dir(self, _):
        raise RuntimeError('Setting backend is disallowed')

    def clear(self, verbose=1):
        if not self._separate_dirs:
            self._backend._clear(verbose=verbose)
        else:
            self.pp.clear(verbose)
            self.tl.clear(verbose)
            self.pl.clear(verbose)

    def __repr__(self):
        return f"{self.__class__.__name__}(root={self._root_dir}, ext='{self._ext}')"
