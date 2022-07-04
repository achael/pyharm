# Functions indexing a results directory

import os
import numpy as np
import h5py

from .grid import Grid
from .util import i_of
from .variables import fns_dict

# Specifically for reading the header as copied/output to result files
from .io.iharm3d_header import read_hdr
from .io import read_log

def smoothed(a, window_sz=101):
    """A potentially reasonably fast smoothing operation.
    Averages only available data, i.e. only half window-size at edges.
    """
    ret = np.array([np.mean(a[:n+window_sz//2]) for n in range(1,window_sz//2+1)])
    ret = np.append(ret, np.convolve(a, np.ones(window_sz), 'valid') / window_sz)
    ret = np.append(ret, np.array([np.mean(a[-n-window_sz//2:]) for n in range(1,window_sz//2+1)]))
    return ret

class AnaResults(object):
    """Tools for dealing with the results computed by scripts/pyharm-analysis.

    Results are organized by remaining independent variable -- so, a phi- and time-average
    will be under 'rth' since these are its remaining independent variables.

    This is more logical than it sounds: this way, most quantity names contain all the info
    needed to plot them.  Also, multiple reductions of the same quantity can be stored with
    logical names, e.g. a phi,t average of rho in 'rth/rho' and a further average over th
    in 'r/rho'.  Basic EH fluxes are e.g. t/Mdot or t/phi_b. Suffix '_per' normalizes
    dimensionless values to the un-smoothed accretion rate, which can increase their
    variability.

    When using __getitem__ (i.e. res[]), the name after the slash doesn't have to be something
    directly present in the file -- it can include many of the 'key' features available in FluidDump,
    notably unary operators (sqrt_, abs_, etc), but this is all separate functions so YMMV.
    Time-dependent variables may also append '_smoothed' or '_smoothed_xx' to calculate a
    running average over xx values (that is, samples, not simulation time units).

    A few variables (beta, sigma, etc) should be suffixed '_post' if they should be calcuated
    by AnaResults.  This is because versions calculated and averaged per-zone might also
    be present in the reductions file.

    For a little more control, you can call res.get_result() specifically, which takes a few
    more arguments documented there.
    """

    diag_fn_common = {# Standard names for some EH fluxes
                    'phi_b_per': lambda diag: diag['Phi_b'] / np.sqrt(diag['mdot']),
                    'phi_b': lambda diag: diag['Phi_b'] / np.sqrt(diag['smooth_mdot']),
                    'phi_b_upper': lambda diag: diag['Phi_b_upper'] / np.sqrt(diag['smooth_mdot']),
                    'phi_b_lower': lambda diag: diag['Phi_b_lower'] / np.sqrt(diag['smooth_mdot']),
                    'edot_per': lambda diag: diag['Edot'] / diag['mdot'],
                    'edot': lambda diag: diag['Edot'] / diag['smooth_mdot'],
                    'ldot_per': lambda diag: diag['Ldot'] / diag['mdot'],
                    'ldot': lambda diag: diag['Ldot'] / diag['smooth_mdot'],
                    # Post-processing functions for fluxes
                    'eff': lambda diag: np.abs(diag['Edot'] - diag['mdot']) / diag['smooth_mdot'],
                    'eff_per': lambda diag: np.abs(diag['Edot'] - diag['mdot']) / diag['mdot'],
                    }
    # How to load variables from a KHARMA .hst file dictionary
    diags_hst = {'mdot': lambda diag: np.abs(diag['Mdot_EH_Flux']),
                 'Phi_b': lambda diag: diag['Phi_EH'],
                 'Edot': lambda diag: diag['Edot_EH'],
                 'Ldot': lambda diag: diag['Ldot_EH']}
    # How to load from analysis results
    diags_ana = {'mdot': lambda diag: np.abs(diag['Mdot'])}

    def __init__(self, fname, tag=""):
        self.tag = tag
        self.cache = {}
        if isinstance(fname, str):
            # When reading HDF5 files, just open the file
            # When reading diagnostic output, read the whole thing
            self.fname = fname
            if ".h5" in fname or ".hdf5" in fname:
                # Read analysis results
                self.file = h5py.File(fname, "r")
                self.ftype = "ana"
                self.diag_fns = {**self.diag_fn_common, **self.diags_ana}
                self.params = read_hdr(self.file['/header'])
                self.grid = Grid(self.params)
                if 'avg/start' in self.file: 
                    self.avg_ends = (self.file['avg/start'][()], self.file['avg/end'][()])
            else:
                # Read diagnostic output.  Much more limited functionality here,
                # mostly for applying diag_fns
                self.file = read_log(fname)
                self.diag_fns = {**self.diag_fn_common, **self.diags_hst}
                self.ftype = "hst"
                self.params = {}
        else:
            # Build an object around existing data dict, passed as "fname"
            self.fname = ""
            self.file = fname
            self.diag_fns = {**self.diag_fn_common, **self.diags_hst}
            self.ftype = "hst"
            self.params = {}
            

    def __del__(self):
        if 'file' in self.__dict__ and not isinstance(self.file, dict):
            self.file.close()

    def __getitem__(self, key):
        """This operates a bit differently from its analog in FluidDump.

        Variables in reductions are specified by remaining independent variable, e.g.
        r/FM_disk for the mass flux in the "disk" region as a function of r (that is,
        summed over th & phi and averaged over t).  See the class description for details.

        Alternatively, you can specify independent variables alone to get them.
        (you can also get such things directly from res.grid!).  AnaResult will
        attempt to look for a dependent variable specified alone, but resolves any
        ambiguities by returning whatever it finds first.
        """
        #print("item ", key)
        # Selection of independent variables, catch-all is below
        # Mostly trying to avoid parameters dict stepping on common letter combinations
        if key in ('r', 'th', 'phi', 't', 'rth', 'thphi', 'rphi', 'rt', 'tht', 'phit', 'rtht', 'rphit'):
            ret_i = self.get_ivar(key)
            if len(ret_i) == 1:
                return ret_i[0]
            else:
                return ret_i
        if key in self.params:
            return self.params[key]
        elif '/' in key:
            return self.get_dvar(*key.split("/"))
        else:
            try:
                # The only independent variable in diagnostic output is t
                #print("Getting t ",key)
                return self.get_dvar('t', key)
            except (IOError, OSError):
                if self.ftype == "ana":
                    # Try to return it from anywhere & everywhere
                    for ivar in self.ivars_present():
                        try:
                            return self.get_dvar(ivar, key)
                        except (IOError, OSError):
                            pass
                ret_i = self.get_ivar(key)
                if len(ret_i) == 1:
                    return ret_i[0]
                else:
                    return ret_i

    def get_result(self, ivar, dvar, **kwargs):
        return (*self.get_ivar(ivar, **kwargs), self.get_dvar(ivar, dvar))

    def get_ivar(self, ivar, th_r=None, mesh=True):
        """Get a list of grids of independent variable values.
        'ivar' must be a string containing the desired combination of r, th or hth (half theta), phi, or t.
        The list must always follow the above ordering and cannot contain >2 variables (use grid)

        examples: 'rt', 't', 'hth', 'rth'

        Always returns a list, e.g. the array of timestamps will be res.get_ivar('t')[0]
        """
        #print("ivar ", ivar)
        # Throw an error if we messed up
        if ivar.replace('r','').replace('t','').replace('h','').replace('phi','') != '':
            raise IOError("Bad ivar: {}. Must be an unseparated list of dimensions r,th,phi,t".format(ivar))

        # Only cache/read the default case
        if th_r is None and mesh == True and ivar in self.cache:
            return self.cache[ivar]

        if ivar not in ('t', 'time'):
            # Don't cache grid for e.g. fluxes
            G = self.grid

        # 2D space: get x,y from grid
        # This ensures rth is correct & potentially is faster
        if ivar == 'rth':
            ret_grids = G.get_xz_locations(mesh=mesh, half_cut=True)
        elif ivar == 'rphi':
            ret_grids = G.get_xy_locations(mesh=mesh)
        elif ivar == 'thphi':
            if th_r is not None:
                r1d = G.coords.r(G.coord_all(mesh=mesh)[:, :, 0, 0])
                at = i_of(r1d, th_r)
            else:
                at = -1
            ret_grids = G.get_thphi_locations(at, mesh=mesh) # Allow bottom & project?
        else:
            # Otherwise, add individual axes to a list & meshgrid them
            ret_i = []
            if ivar[-1:] == 't' or ivar == 'diag':
                if self.ftype == "hst":
                    t = self.file['time'][()]
                else:
                    if ivar == 'diag':
                        t = self.file['diag/time'][()]
                    else:
                        t = self.file['coord/t'][()]

                # Correct any mishaps with array ordering
                self.t_perm = np.argsort(t)
                t = t[self.t_perm]

                # For 2D r vs t plots or similar.  ONLY 2D though
                if mesh and ivar != 't' and ivar != 'diag':
                    t = np.append(t, t[-1] + (t[-1] - t[0]) / t.shape[0])

                ret_i.append(t)
                ivar = ivar[:-1]
            else:
                # This is the case of 1D spatial variable, which will never
                # need to be a mesh
                mesh = False

            if ivar != '':
                # 1D space: use spherical coordinates directly
                native_coords = G.coord_all(mesh=mesh)
                if ivar == 'r':
                    ret_i.append(G.coords.r(native_coords[:, :, 0, 0]))
                elif ivar in ('th', 'hth'):
                    if th_r is not None:
                        r1d = G.coords.r(native_coords[:, :, 0, 0])
                        th = G.coords.th(native_coords[:, i_of(r1d, th_r), :, 0])
                    else:
                        th = G.coords.th(native_coords[:, -1, :, 0])
                    if ivar == 'hth':
                        th = th[:len(th)//2]
                    ret_i.append(th)
                elif ivar == 'phi':
                    ret_i.append(G.coords.phi(native_coords[:, 0, 0, :]))

            ret_grids = np.meshgrid(*reversed(ret_i))
            ret_grids.reverse()

        if th_r is None and mesh == True:
            self.cache[ivar] = ret_grids
        return ret_grids

    def get_dvar(self, ivar, dvar):
        """Takes an independent and dependent variable name, and attempts to read or derive
        the appropriate reduction of the dependent variable.
        Usually, this is called from __getitem__.

        In implementation, this is the closest analog to FluidDump's __getitem__ function -- it's the
        place to add any complex new tags/operations/whatever.
        """
        #print("dvar ", dvar)

        # Cache based on *both* variables to avoid collisions e.g. t/Mdot vs rt/Mdot or something
        vname = ivar+"/"+dvar
        if vname in self.cache:
            return self.cache[vname]

        # Grab from the file first no matter what it's named
        if ivar in self.file and dvar in self.file[ivar]:
            ret_v = self.file[ivar][dvar][()]
            if 't' in ivar:
                # Ensure time-ordering is computed
                self.get_ivar('t')
                # Apply ordered-time permutation on read and never after
                ret_v = ret_v[self.t_perm]
        elif self.ftype == "hst" and dvar in self.file:
            ret_v = self.file[dvar]
        # Prefixes for a few common 1:1 math operations
        elif dvar[:5] == "sqrt_":
            ret_v = np.sqrt(self.get_dvar(ivar, dvar[5:]))
        elif dvar[:7] == "square_":
            ret_v = self.get_dvar(ivar, dvar[7:])**2
        elif dvar[:4] == "abs_":
            ret_v = np.abs(self.get_dvar(ivar, dvar[4:]))
        elif dvar[:4] == "log_":
            ret_v = np.log10(self.get_dvar(ivar, dvar[4:]))
        elif dvar[:3] == "ln_":
            ret_v = np.log(self.get_dvar(ivar, dvar[3:]))
        elif dvar[:4] == "inv_":
            ret_v = 1/self.get_dvar(ivar, dvar[4:])
        elif dvar[:6] == "smooth": # smooth_ or e.g. smooth101_
            dvarl = dvar.split('_')
            op = dvarl[0]
            try:
                # Parse argument
                window = int(op[6:])
            except (ValueError):
                # Default
                window = 101
            ret_v = smoothed(self.get_dvar(ivar, '_'.join(dvarl[1:])), window_sz=window)
        elif 'sigma_post' in dvar:
            ret_v = (self.get_dvar(ivar, dvar.replace('sigma_post','bsq')) /
                    self.get_dvar(ivar, dvar.replace('sigma_post','rho')))
        elif 'beta_post' in dvar:
            ret_v = (2 * self.get_dvar(ivar, dvar.replace('beta_post','Pg')) /
                    self.get_dvar(ivar, dvar.replace('beta_post','bsq')))
        elif 'bsq' in dvar:
            ret_v = self.get_dvar(ivar, dvar.replace('bsq','b'))**2
        elif 'Theta_post' in dvar:
            ret_v = (self.get_dvar(ivar, dvar.replace('Theta_post','Pg')) /
                    self.get_dvar(ivar, dvar.replace('Theta_post','rho')))
        elif dvar in self.diag_fns:
            ret_v = self.diag_fns[dvar](self)
        else:
            raise IOError("Can't find variable: {} as a function of {}".format(dvar, ivar))
        
        self.cache[vname] = ret_v
        return ret_v

    def ivars_present(self):
        if self.ftype == "ana":
            return [key for key in self.file.keys() if key not in ['avg', 'coord', 'extras', 'header', 'pdf', 'pdft']]
        elif self.ftype == "hst":
            return self.file.keys()

    def dvars_present(self, ivar=None):
        if self.ftype == "ana":
            if ivar is not None:
                return self.file[ivar].keys()
            else:
                keys = []
                for ivar in self.ivars_present():
                    keys.extend(self.file[ivar].keys())
                return keys

        elif self.ftype == "hst":
            return self.file.keys()
    
    def get_time_slice(self, tmin, tmax=0):
        """Get the indices in the (correct, potentially reordered) timeline
        corresponding to stated tmin, tmax.
        Allows negative tmin to specify a slice to the end of the run
        """
        tmin = float(tmin)
        tmax = float(tmax)
        if tmin > 0 and tmax > 0:
            i_begin = i_of(self['t'], tmin)
            i_end = i_of(self['t'], tmax)
        elif tmin < 0:
            i_begin = i_of(self['t'], self['t'][-1] + tmin)
            i_end = None

        return slice(i_begin, i_end)

