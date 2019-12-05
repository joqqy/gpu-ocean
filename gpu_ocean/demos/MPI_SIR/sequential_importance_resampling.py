# -*- coding: utf-8 -*-

"""
This python class represents an ensemble of ocean models with slightly
perturbed states. Runs on multiple nodes using MPI

Copyright (C) 2018  SINTEF ICT

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import numpy as np
from netCDF4 import Dataset
from mpi4py import MPI
import gc, os, sys
import time

# Needed for clean exit with logging enabled
import pycuda.driver as cuda

#Import our simulator
file_dir = os.path.dirname(os.path.realpath(__file__)) 
sys.path.insert(0, os.path.abspath(os.path.join(file_dir, '../../')))

from SWESimulators import CDKLM16, PlotHelper, Common, WindStress, OceanographicUtilities, MPIOceanModelEnsemble, NetCDFInitialization
from SWESimulators import DataAssimilationUtils as dautils

def testMPI():
    comm = MPI.COMM_WORLD

    print("Hello! I'm rank %d from %d running in total..." % (comm.rank, comm.size))

    #Make results deterministic
    #np.random.seed(seed=(42 + comm.rank))
    comm.Barrier()   
    
    
    

def generateDrifterPositions(data_args, num_drifters):
    """Randomly placed drifters, avoiding land."""
    assert(MPI.COMM_WORLD.rank == 0)
    
    drifters = np.empty((num_drifters, 2))
    
    for id in range(num_drifters):
        land = True
        while(land is True):
            x = np.random.randint(0, data_args['nx'])
            y = np.random.randint(0, data_args['ny'])
            
            if(data_args['hu0'][y][x] is not np.ma.masked and
               data_args['hu0'][y-1][x] is not np.ma.masked and data_args['hu0'][y+1][x] is not np.ma.masked and
               data_args['hu0'][y][x-1] is not np.ma.masked and data_args['hu0'][y][x+1] is not np.ma.masked):
                drifters[id, 0]  = x * data_args['dx']
                drifters[id, 1]  = y * data_args['dy']
                land = False

    return drifters
    
def dataAssimilationLoop(ensemble, resampling_times, resampling=True):
    #Perform actual data assimilation        
    t = 0
    
    observation_times = ensemble.observations.get_observation_times()
    
    for i, resampling_time in enumerate(resampling_times):
        #Step all nodes in time to next assimilation stage
        assimilation_dt = resampling_time - t
        
        step_size = 60
        #FIXME: Assert: assimilation_dt divisable with step_size
        sub_steps = int(assimilation_dt // step_size)
        
        for j in range(sub_steps):
            t = ensemble.modelStep(step_size, update_dt=False)
            
            # Find latest observed drifters
            obs_index = min(0, np.searchsorted(observation_times, t) - 1)
            drifter_cells = ensemble.getDrifterCells(observation_times[obs_index])
            
            #Make ParticleInfo for writing to file 
            ensemble.dumpParticleSample(drifter_cells)
        
        if(resampling):
            #Gather the gaussian weights from all nodes to a global vector on rank 0
            global_normalized_weights = ensemble.getNormalizedWeights()
        
            #Resample the particles
            ensemble.resampleParticles(global_normalized_weights, write_to_file=True)
        
        ensemble.updateDt()
        
        print(str(ensemble.comm.rank) + ", ", end="", flush=True)
    
def forecastLoop(ensemble, end_t):      
    start_t = ensemble.t
    sub_step_size = 60
    
    observation_times = ensemble.observations.get_observation_times()
    
    start_t = np.round(start_t)
    end_t = np.round(end_t)
    
    for dump_time in range(int(start_t), int(end_t), sub_step_size):
        t = ensemble.modelStep(sub_step_size, update_dt=False)
        
        if(dump_time % 3600 == 0):
            ensemble.updateDt()
        
        # Find latest observed drifters
        obs_index = min(0, np.searchsorted(observation_times, t) - 1)
        drifter_cells = ensemble.getDrifterCells(observation_times[obs_index])
        
        # Store observation from forecast
        ensemble.dumpForecastParticleSample()
        ensemble.dumpParticleSample(drifter_cells)
            
    print(str(ensemble.comm.rank) + ", ", end="", flush=True)
    
def setupLogger(args):
    import logging

    #Get root logger
    logger = logging.getLogger('')
    logger.setLevel(args.log_level)

    #Add log to screen
    ch = logging.StreamHandler()
    ch.setLevel(args.log_level)
    logger.addHandler(ch)
    logger.log(args.log_level, "Console logger using level %s", logging.getLevelName(args.log_level))
            
    #Get the logfilename (try to evaluate if Python expression...)
    try:
        log_file = eval(args.logfile, self.shell.user_global_ns, self.shell.user_ns)
    except:
        log_file = args.log_file
            
    #Add log to file
    logger.log(args.log_level, "File logger using level %s to %s", logging.getLevelName(args.log_level), log_file)
    fh = logging.FileHandler(log_file)
    formatter = logging.Formatter('%(asctime)s:%(name)s:%(levelname)s: %(message)s')
    fh.setFormatter(formatter)
    fh.setLevel(args.log_level)
    logger.addHandler(fh)
        
    logger.info("Python version %s", sys.version)
    
    
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Run an MPI cluster for sequential importance resampling. Run this file using e.g., using mpiexec -n 4 python sequential_importance_resampling.py to run a simulation. Plot results using the --make_plots option (without MPI!)')
    parser.add_argument('-dt', type=float, default=0.0)
    parser.add_argument('--per_node_ensemble_size', type=int, default=5)
    parser.add_argument('--no_resampling', action='store_true') # default: False
    parser.add_argument('--observation_variance', type=float, default=1)#0.01**2)
    parser.add_argument('--observation_file', type=str, default=None)
    parser.add_argument('--log_file', type=str, default="sequential_importance_resampling.log")
    parser.add_argument('--log_level', type=int, default=20)
    parser.add_argument('--resampling_frequency_minutes', type=int, default=15)
    parser.add_argument('--data_assimilation_end_hours', type=int, default=6)
    parser.add_argument('--forecast_duration_hours', type=int, default=6)
    
    
    
    args = parser.parse_args()
    
    logger = setupLogger(args)
    
    resampling = not args.no_resampling

        
    if True:
        #Test that MPI works
        testMPI()
        
        
        # All observation files are sampled on a 5 minute basis
        assert(args.resampling_frequency_minutes % 5 == 0), 'resampling_frequency_minutes should be a factor of 5'
        obs_config_observation_interval = int(args.resampling_frequency_minutes / 5) 
        
        data_assimilation_end_t = args.data_assimilation_end_hours * 60*60
        
        # Make list of resampling times - make sure that we include the end time as well
        resampling_times = np.arange(args.resampling_frequency_minutes, args.data_assimilation_end_hours*60 + 1, args.resampling_frequency_minutes)*60
        
        # Find the end time for the forecast
        end_t_forecast = resampling_times[-1] + args.forecast_duration_hours * 60*60
        
        
        # FIXME: Hardcoded parameters
        #observation_type = dautils.ObservationType.StaticBuoys
        observation_type = dautils.ObservationType.UnderlyingFlow
        
        kwargs = {}
        #Generate initial conditions on rank 0
        if (MPI.COMM_WORLD.rank == 0):
            
            #Size of ensemble per node
            kwargs['local_ensemble_size'] = args.per_node_ensemble_size
            
            #Initial conditions
            # FIXME: Hardcoded parameters
            casename = 'lovese'
            source_url = [
                'https://thredds.met.no/thredds/dodsC/fou-hi/norkyst800m-1h/NorKyst-800m_ZDEPTHS_his.an.2019071600.nc',
                'https://thredds.met.no/thredds/dodsC/fou-hi/norkyst800m-1h/NorKyst-800m_ZDEPTHS_his.an.2019071700.nc',
                'https://thredds.met.no/thredds/dodsC/fou-hi/norkyst800m-1h/NorKyst-800m_ZDEPTHS_his.an.2019071800.nc',
                'https://thredds.met.no/thredds/dodsC/fou-hi/norkyst800m-1h/NorKyst-800m_ZDEPTHS_his.an.2019071900.nc',
                'https://thredds.met.no/thredds/dodsC/fou-hi/norkyst800m-1h/NorKyst-800m_ZDEPTHS_his.an.2019072000.nc',
                'https://thredds.met.no/thredds/dodsC/fou-hi/norkyst800m-1h/NorKyst-800m_ZDEPTHS_his.an.2019072100.nc'
            ]
            
            # Reduce to only relevant files:
            required_num_files = np.int(np.floor(end_t_forecast/(24*60*60) )) + 1
            source_url = source_url[:required_num_files]
            
            dt = args.dt
            
            t0 = time.time()
            full_data_args = NetCDFInitialization.getInitialConditionsNorKystCases(source_url, casename)
            data_args = NetCDFInitialization.removeMetadata(full_data_args)
            t1 = time.time()
            total = t1-t0
            print("Fetched initial conditions in " + str(total) + " s")
            
            sim_args = {
                "dt": dt,
                "rk_order": 2,
                "desingularization_eps": 1.0,
                "small_scale_perturbation": False,
                #"small_scale_perturbation_amplitude": 1.0e-5, #1.0e-5,
                #"small_scale_perturbation_interpolation_factor": 1,
                "subsample_angle": None,
                "subsample_f": None,
                "write_netcdf": False,
            }
            
            kwargs['data_args'] = data_args
            kwargs['sim_args'] = sim_args
            
        #Arguments sent to the ensemble (OceanModelEnsemble)
        kwargs['ensemble_args'] = {
            'observation_variance': args.observation_variance, 
            #'initialization_variance_factor_ocean_field': args.initialization_variance_factor_ocean_field
        }
            
        #Create ensemble on all nodes
        t0 = time.time()
        ensemble = MPIOceanModelEnsemble.MPIOceanModelEnsemble(MPI.COMM_WORLD, args.observation_file, observation_type, **kwargs, metadata=vars(args))
        t1 = time.time()
        total = t1-t0
        print("Initialized MPI ensemble on rank " + str(MPI.COMM_WORLD.rank) + " in " + str(total) + " s")
        
        #ensemble.setBuoySet([26])
        ensemble.setDrifterSet([11])
        ensemble.observations.setObservationInterval(obs_config_observation_interval)
        
        for particleInfo in ensemble.ensemble.particleInfos:
            particleInfo.usePredefinedExtraCellsLovese()
            
        
        
        if (ensemble.comm.rank == 0):
            print("Will resample at times: ", resampling_times)
            
        if resampling_times[-1] > 0:
            t0 = time.time()
            dataAssimilationLoop(ensemble, resampling_times, resampling=resampling)
            t1 = time.time()
            total = t1-t0
            print("Data assimilation loop on rank " + str(MPI.COMM_WORLD.rank) + " finished in " + str(total) + " s")
        else:
            print("No resampling times found - rank " + str(MPI.COMM_WORLD.rank) + " moving directly to forecast")
        
        ensemble.initDriftersFromObservations()
        
        t0 = time.time()
        forecastLoop(ensemble, end_t_forecast)
        t1 = time.time()
        total = t1-t0
        print("Forecast loop on rank " + str(MPI.COMM_WORLD.rank) + " finished in " + str(total) + " s")
        
        # Write to files
        ensemble.dumpParticleInfosToFiles()
        ensemble.dumpDrifterForecastToFiles()
        
        # Handle CUDA context when exiting python
        import atexit
        def exitfunc():
            import logging
            logger =  logging.getLogger(__name__)
            logger.info("[" + str(MPI.COMM_WORLD.rank) + "]: Exitfunc: Resetting CUDA context stack")
            while (cuda.Context.get_current() != None):
                context = cuda.Context.get_current()
                logger.info("`-> Popping <%s>", str(context.handle))
                cuda.Context.pop()
            logger.debug("==================================================================")
            logging.shutdown()
            
        atexit.register(exitfunc)
