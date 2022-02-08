
import numpy as np
import h5py

from .. import parameters
from ..util import slice_to_index
from ..grid import Grid
from .interface import DumpFile

# This is where we drop in the Parthenon reader
from .phdf import phdf

class KHARMAFile(DumpFile):
    """File filter for KHARMA files"""
    # Names which aren't directly prims.x or cons.x, but which we can translate
    prim_names_dict = {"RHO": "rho",
                       "UU": "u",
                       "U1": "u1",
                       "U2": "u2",
                       "U3": "u3",
                       "KTOT": "Ktot",
                       "KEL_KAWAZURA": "Kel_Kawazura",
                       "KEL_WERNER":   "Kel_Werner",
                       "KEL_ROWAN":    "Kel_Rowan",
                       "KEL_SHARMA":   "Kel_Sharma",
                       "KEL_CONSTANT": "Kel_Constant"}

    @classmethod
    def get_dump_time(cls, fname):
        """Quickly get just the simulation time represented in the dump file.
        For cutting on time without loading everything
        """
        with h5py.File(fname, 'r') as dfile:
            if 'Info' in dfile.keys():
                return dfile['Info'].attrs['Time']
        return None

    def kharma_standardize(self, var):
        """Standardize the names we're asked for, so we cache only one copy"""
        # Translate certain caps
        if var in self.prim_names_dict.keys():
            var = self.prim_names_dict[var]

        # Pick out indices and return their vectors
        ind = None
        if var[-1:] in ("1", "2", "3"):
            # Mark which index we want
            ind = int(var[-1:]) - 1
            # Then read the corresponding vector, cons/prims.u/B
            var = var[:-2] + ("B" if "B" in var[-2:] else "uvec")

        # Extend the shorthand for primitive variables to their full names in KHARMA,
        # but not other variables.
        if var in ("rho", "u", "uvec", "B"):
            var = "prims."+var
        if ("Kel" in var or "Ktot" in var) and ("cons" not in var):
            var = "prims."+var
        
        return var, ind

    def __init__(self, filename, ghost_zones=False, params=None):
        """Create an Iharm3DFile object -- note that the file handle will stay
        open as long as the object
        """
        self.fname = filename
        self.cache = {}
        if params is None:
            self.params = self.read_params()
            self.params['ghost_zones'] = ghost_zones
            self.params['ng'] = ghost_zones * self.params['ng_file']
            if ghost_zones and self.params['ng_file'] == 0:
                raise ValueError("Ghost zones aren't available in file {}".format(self.fname))
        else:
            self.params = params

    def read_params(self):
        # TODO if reading very old output, fall back to text .par file in dumps folder
        # TODO there's likely a nice way to get all this from phdf
        fil = phdf(self.fname)
        if 'Input' in fil.fid:
            par_string = fil.fid['Input'].attrs['File']
            if type(par_string) != str:
                par_string = par_string.decode("UTF-8")
            params = parameters.parse_parthenon_dat(par_string)
        else:
            raise RuntimeError("No parameters could be found in KHARMA dump {}".format(self.fname))

        # Use Parthenon's reader for the file-specific stuff
        params['ng_file'] = fil.NGhost * fil.IncludesGhost
        # Set incidental parameters from what we've read
        params['t'] = fil.Time
        params['n_step'] = fil.NCycle
        # Add dump number if we've conformed to usual naming scheme
        fname_parts = self.fname.split("/")[-1].split(".")
        if len(fname_parts) > 2:
            try:
                params['n_dump'] = int(fname_parts[2])
            except ValueError:
                # dumps named 'final'.  Drop them?
                params['n_dump'] = fname_parts[2]

        fil.fid.close()
        del fil
        return params

    def read_var(self, var, astype=None, slc=()):
        var, ind = self.kharma_standardize(var)
        if var in self.cache:
            if ind is not None:
                return self.cache[var][ind]
            else:
                return self.cache[var]

        # Open file
        fil = phdf(self.fname)

        # All primitives/conserved. We added this to iharm3d, special case it here
        if var == "prims":
            return np.array([self.read_var(v2) for v2 in fil.Variables if "prims" in v2])
        elif var == "cons":
            return np.array([self.read_var(v2) for v2 in fil.Variables if "cons" in v2])

        if var not in fil.Variables and self.index_of(var) is None:
            raise IOError("Cannot find variable "+var+" in dump "+self.fname+". Should it have been calculated?")

        params = self.params
        # Recall ng=0 if ghost_zones is False.  Thus this says:
        # if we want ghost zones, set them in nontrivial dimensions
        ng_ix = params['ng']
        ng_iy = params['ng'] if params['n2'] > 1 else 0
        ng_iz = params['ng'] if params['n3'] > 1 else 0
        ntot = [params['n1']+2*ng_ix, params['n2']+2*ng_iy, params['n3']+2*ng_iz]
        # Even if we don't want ghosts, we'll potentially need to cut them from the file
        ng_fx = params['ng_file']
        ng_fy = params['ng_file'] if params['n2'] > 1 else 0
        ng_fz = params['ng_file'] if params['n3'] > 1 else 0
        # Finally, we need to decipher where to put each meshblock vs the whole grid,
        # which we do inelegantly using zone locations
        dx = params['dx1']
        dy = params['dx2']
        dz = params['dx3']
        startx = (0., params['startx1'], params['startx2'], params['startx3'])

        # What locations do we want to read from the file?
        # Get a start and end point based on the total size and slice
        #print("Reading file slice ", slc)
        file_start, file_stop = slice_to_index((0, 0, 0), ntot, slc)
        out_shape = [file_stop[i] - file_start[i] for i in range(len(file_stop))]

        #print("Reading indices ", file_start, " to ", file_stop, " shape ", out_shape)

        # Allocate the full mesh size
        if "jcon" in var:
            out = np.zeros((4, *out_shape), dtype=astype)
        elif "B" in var or "uvec" in var: # We cache the whole thing even for an index
            out = np.zeros((3, *out_shape), dtype=astype)
        else:
            out = np.zeros(out_shape, dtype=astype)

        # The slice we need of each block is just ng to -ng, with 0->None for the whole slice
        o = [None if i == 0 else i for i in [ng_fx, -ng_fx, ng_fy, -ng_fy, ng_fz, -ng_fz]]

        # Arrange and read each block
        for ib in range(fil.NumBlocks):
            bb = fil.BlockBounds[ib]
            # Internal location of the block i.e. starting/stopping indices in the final, big mesh
            # First, take the start/stop locations and map them to integers
            # We only need to add ghost zones here if the file has them *and* we want them:
            # If so, each block will be 2*ng bigger, but we'll want to handle indices from zero regardless
            b = (slice(int((bb[0]+dx/2 - startx[1])/dx), int((bb[1]+dx/2 - startx[1])/dx)+2*ng_ix),
                 slice(int((bb[2]+dy/2 - startx[2])/dy), int((bb[3]+dy/2 - startx[2])/dy)+2*ng_iy),
                 slice(int((bb[4]+dz/2 - startx[3])/dz), int((bb[5]+dz/2 - startx[3])/dz)+2*ng_iz))
            # Intersect block's global bounds with our desired slice: this is where we're outputting to
            out_slc = tuple([slice(max(b[i].start, file_start[i]), min(b[i].stop, file_stop[i])) for i in range(3)])
            # Subtract off the block's global starting point: this is what we're taking from
            # If the ghost zones are included (ng_f > 0) but we don't want them (all) (ng_i = 0),
            # then take a portion of the file.  Otherwise take it all.
            # Also include the block number out front
            fil_slc = (ib, slice(out_slc[2].start - b[2].start + ng_fz - ng_iz, out_slc[2].stop - b[2].start - ng_fz + ng_iz),
                           slice(out_slc[1].start - b[1].start + ng_fy - ng_iy, out_slc[1].stop - b[1].start - ng_fy + ng_iy),
                           slice(out_slc[0].start - b[0].start + ng_fx - ng_ix, out_slc[0].stop - b[0].start - ng_fx + ng_ix))
            for i in range(1,4):
                if fil_slc[i].start > fil_slc[i].stop:
                    # Don't read blocks outside our domain
                    continue
            #print("Reading block: ", b, " to location ", out_slc, " by reading block portion ", fil_slc)

            if 'prims.rho' in fil.Variables:
                # New file format. Read whatever
                if len(out.shape) == 4: # Always read the whole vector, even if we're returning an index
                    out[[slice(None),] + out_slc] = fil.Get(var, False)[fil_slc + [slice(None),]].T
                else: # Read a scalar
                    out[out_slc] = fil.Get(var, False)[fil_slc].T

            else:
                # Old file formats.  If we'd split prims/B_prim:
                if "B" in var and 'c.c.bulk.B_prim' in fil.Variables:
                        out[[slice(None),] + out_slc] = fil.Get('c.c.bulk.B_prim', False)[fil_slc + [slice(None),]].T
                else:
                    i = self.index_of(var)
                    if i is None:
                        # We're not grabbing anything except primitives from old KHARMA files.
                        raise IOError("Cannot find variable "+var+" in file "+self.fname+"!")
                    elif type(i) == int:
                        out[out_slc] = fil.Get('c.c.bulk.prims', False)[fil_slc + (i,)].T
                    else:
                        out[Ellipsis, out_slc] = fil.Get('c.c.bulk.prims', False)[fil_slc + (i,)].T
        # Close
        fil.fid.close()
        del fil

        # We keep 3 indices for file reads, but if we should lose one, do it
        self.cache[var] = np.squeeze(out)
        if ind is not None:
            return self.cache[var][ind]
        else:
            return self.cache[var]

