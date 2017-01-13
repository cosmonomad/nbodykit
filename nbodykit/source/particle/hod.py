from nbodykit import CurrentMPIComm
from nbodykit.base.particles import column
from nbodykit.source.particle.from_numpy import Array
from nbodykit.utils import GatherArray, ScatterArray

from halotools.empirical_models import HodModelFactory, model_defaults

from nbodykit.extern.six import add_metaclass
import abc
import logging
import numpy

def remove_object_dtypes(data):
    """
    Utility function to convert 'O' data types to strings
    """
    for col in data.colnames:
        if data.dtype[col] == 'O':
            data[col] = data[col].astype('U')
    return data
        
@add_metaclass(abc.ABCMeta)
class HODBase(Array):
    """
    A base class to be used for HOD population of a halo catalog.
    
    The user must supply the :func:`_makemodel` function, which returns
    the halotools composite HOD model. 
    
    This abstraction allows the user to potentially implement several 
    different types of HOD models quickly, while using the population 
    framework of this base class.
    """
    logger = logging.getLogger("HODBase")
    
    @CurrentMPIComm.enable
    def __init__(self, halos, cosmo, redshift, mdef, 
                  rsd=None, seed=None, use_cache=False, comm=None, **params):
        
        from halotools.sim_manager import UserSuppliedHaloCatalog
        if not isinstance(halos, UserSuppliedHaloCatalog):
            raise TypeError("input 'halos' object for HOD should be a halotools UserSuppliedHaloCatalog")
            
        if rsd is None:
            rsd = [0, 0, 0.]
        
        # store the halotools catalog
        self._halos = halos
        self.cosmo  = cosmo
        self.comm   = comm
        
        # grab the BoxSize from the halotools catalog
        self.attrs['BoxSize'] = numpy.empty(3)
        self.attrs['BoxSize'][:] = halos.Lbox
        
        # store mass and radius keys
        self.mass   = model_defaults.get_halo_mass_key(mdef)
        self.radius = model_defaults.get_halo_boundary_key(mdef)
    
        # propapagate all columns in the halo catalogs to the galaxy catalog
        model_defaults.default_haloprop_list_inherited_by_mock = halos.halo_table.colnames
                        
        # store the attributes
        self.attrs['mdef'] = mdef
        self.attrs['redshift'] = redshift
        self.attrs['rsd'] = rsd
        self.attrs['seed'] = seed
        self.attrs.update(params)
        self.attrs.update({'cosmo.%s' %k : cosmo[k] for k in cosmo})
        
        # make the model!
        self._model = self._makemodel()
                                    
        # set the HOD params
        for param in self._model.param_dict:
            if param not in self.attrs:
                raise ValueError("missing '%s' parameter when initializing HOD" %param)
            self._model.param_dict[param] = self.attrs[param]
            
        # make the actual source
        Array.__init__(self, self._makesource(), comm=comm, use_cache=use_cache)
            
        # crash with no particles!
        if self.csize == 0:
            raise ValueError("no particles in catalog after populating HOD")
        
    @abc.abstractmethod
    def _makemodel(self):
        """
        Abstract class to be overwritten by user; this should return
        the HOD model instance that will be used to do the mock 
        population
        """
        pass
        
    def _makesource(self):
        """
        Make the source of galaxies by performing the halo HOD population
        
        .. note:: 
            The mock population is only done by the root, and the resulting
            catalog is then distributed evenly amongst the available ranks
        """
        from astropy.table import Table
        
        # gather all halos to root
        all_halos = GatherArray(self._halos.halo_table.as_array(), self.comm, root=0)
            
        # root does the mock population
        if self.comm.rank == 0:

            # set the halo table on the root to the Table containing all halo
            self._halos.halo_table = remove_object_dtypes(Table(data=all_halos, copy=True))
            del all_halos
                
            # populate 
            self._model.populate_mock(halocat=self._halos, halo_mass_column_key=self.mass,
                                      Num_ptcl_requirement=1, seed=self.attrs['seed'])
            
            # replace any object dtypes
            data = remove_object_dtypes(self._model.mock.galaxy_table).as_array()
            del self._model.mock.galaxy_table
        else:
            data = None
            
        # log the stats
        if self.comm.rank == 0:
            self._log_populated_stats(data)
            
        return ScatterArray(data, self.comm)

    def repopulate(self, seed=None, **params):
        """
        Update the HOD parameters and then re-populate the mock catalog
        
        .. warning::
            This operation is done in-place, so the size of the Source
            changes
        
        Parameters
        ----------
        seed : int; optional
            the new seed to use when populating the mock
        params :
            key/value pairs of HOD parameters to update
        """
        # set the new seed
        self.attrs['seed'] = seed
        
        # update the HOD model parameters
        for name in params:
            if name not in self._model.param_dict:
                valid = list(self._model.param_dict.keys())
                raise ValueError("'%s' is not a valid Hod parameter name; valid are: %s" %(name, str(valid)))
            self._model.param_dict[name] = params[name]
            
        # the root will do the mock population
        if self.comm.rank == 0:
            
            # re-populate the mock (without halo catalog pre-processing)
            self._model.mock.populate(Num_ptcl_requirement=1, 
                                      halo_mass_column_key=self.mass,
                                      seed=self.attrs['seed'])
            
            # replace any object dtypes
            data = remove_object_dtypes(self._model.mock.galaxy_table).as_array()
            del self._model.mock.galaxy_table
            
        else:
            data = None
        
        # log the stats
        if self.comm.rank == 0:
            self._log_populated_stats(data)
        
        # re-initialize with new source
        Array.__init__(self, ScatterArray(data, self.comm), comm=self.comm, use_cache=self.use_cache)
        
    def _log_populated_stats(self, data):
        """
        Internal function to log statistics of the populated catalog
        """
        if len(data) > 0:
            
            fsat = 1.*(data['gal_type'] == 'satellites').sum()/len(data)
            self.logger.info("satellite fraction: %.2f" %fsat)

            logmass = numpy.log10(data[self.mass])
            self.logger.info("mean log10 halo mass: %.2f" %logmass.mean())
            self.logger.info("std log10 halo mass: %.2f" %logmass.std())

    @column
    def Position(self):
        pos = numpy.vstack([self._source['x'], self._source['y'], self._source['z']]).T
        return self.make_column(pos)

    @column
    def Velocity(self):
        vel = numpy.vstack([self._source['vx'], self._source['vy'], self._source['vz']]).T
        return self.make_column(vel)


from halotools.empirical_models import Zheng07Sats, Zheng07Cens
from halotools.empirical_models import NFWPhaseSpace, TrivialPhaseSpace

class HOD(HODBase):
    """
    A `ParticleSource` that uses the HOD prescription of 
    Zheng et al. 2007 to populate an input halo catalog with galaxies, 
    and returns the (Position, Velocity) of those galaxies
    
    The mock population is done using `halotools` (http://halotools.readthedocs.org)
    The HOD model is of the commonly-used form:
    
    Parameters
    ----------
        logMmin : 
            Minimum mass required for a halo to host a central galaxy
        sigma_logM : 
            Rate of transition from <Ncen>=0 --> <Ncen>=1
        alpha : 
            Power law slope of the relation between halo mass and <Nsat>
        logM0 : 
            Low-mass cutoff in <Nsat>
        logM1 : 
            Characteristic halo mass where <Nsat> begins to assume a power law form
    
    See the documentation for the `halotools` builtin Zheng07 HOD model, 
    for further details regarding the HOD
    
    References
    ----------
    Zheng et al. (2007), arXiv:0703457
    """
    logger = logging.getLogger("HOD")
    
    @CurrentMPIComm.enable
    def __init__(self, halos, cosmo, redshift, mdef, logMmin=13.031, sigma_logM=0.38, 
                    alpha=0.76, logM0=13.27, logM1=14.08, rsd=None, 
                    seed=None, use_cache=False, comm=None):
        """
        Initialize the Source. Default HOD values from Reid et al. 2014
                    
        Parameters
        ----------
        halos : halotools.sim_manager.UserSuppliedHaloCatalog
            the halotools table holding the halo data
        cosmo : nbodykit.cosmology.Cosmology
            the cosmology instance, needed to populate the HOD
        redshift : float
            the redshift at which we are populating the HOD
        mdef : str
            string specifying mass definition, used for computing default
            halo radii and concentration; should be 'vir' or 'XXXc' or 
            'XXXm' where 'XXX' is an int specifying the overdensity
        logMmin : float; optional
            Minimum mass required for a halo to host a central galaxy
        sigma_logM : float; optional
            Rate of transition from <Ncen>=0 --> <Ncen>=1
        alpha : float; optional
            Power law slope of the relation between halo mass and <Nsat>
        logM0 : float; optional
            Low-mass cutoff in <Nsat>
        logM1 : float; optional
            Characteristic halo mass where <Nsat> begins to assume a power law form
        rsd : 3-vector; optional
            the RSD direction
        seed : int; optional
            the random seed to generate deterministic mocks
        """
        params = {}
        params['logMmin'] = logMmin
        params['sigma_logM'] = sigma_logM
        params['alpha'] = alpha
        params['logM0'] = logM0
        params['logM1'] = logM1
        
        HODBase.__init__(self, halos, cosmo, redshift, mdef, 
                        rsd=rsd, seed=seed, use_cache=use_cache, comm=comm, **params)

    def _makemodel(self):
        """
        Return the Zheng 07 HOD model
        
        This model evaluates Eqs. 2 and 5 of Zheng et al. 2007
        """
        model = {}
        
        # use concentration from halo table
        if 'halo_nfw_conc' in self._halos.halo_table.colnames:
            conc_mass_model = 'direct_from_halo_catalog'
        # use empirical prescription for c(M)
        else:
            conc_mass_model = 'dutton_maccio14'
    
        # occupation functions
        model['centrals_occupation'] = Zheng07Cens(prim_haloprop_key=self.mass)
        model['satellites_occupation'] = Zheng07Sats(prim_haloprop_key=self.mass, modulate_with_cenocc=True)
        model['satellites_occupation']._suppress_repeated_param_warning = True
    
        # profile functions
        kws = {'cosmology':self.cosmo.engine, 'redshift':self.attrs['redshift'], 'mdef':self.attrs['mdef']}
        model['centrals_profile'] = TrivialPhaseSpace(**kws)
        model['satellites_profile'] = NFWPhaseSpace(conc_mass_model=conc_mass_model, **kws)
    
        return HodModelFactory(**model)
        
    
