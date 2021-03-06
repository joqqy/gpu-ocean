# -*- coding: utf-8 -*-

"""
This software is a part of GPU Ocean.

Copyright (C) 2018  SINTEF Digital

This python class implements a DrifterCollection living on the GPU.

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


from matplotlib import pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import time
import pycuda.driver as cuda

from SWESimulators import Common
from SWESimulators import BaseDrifterCollection

class GPUDrifterCollection(BaseDrifterCollection.BaseDrifterCollection):
    def __init__(self, gpu_ctx, numDrifters, \
                 observation_variance=0.01, \
                 boundaryConditions=Common.BoundaryConditions(), \
                 initialization_cov_drifters=None, \
                 domain_size_x=1.0, domain_size_y=1.0, \
                 gpu_stream=None, \
                 block_width = 64):
        
        super(GPUDrifterCollection, self).__init__(numDrifters,
                                observation_variance=observation_variance,
                                boundaryConditions=boundaryConditions,
                                domain_size_x=domain_size_x, 
                                domain_size_y=domain_size_y)
        
        # Define CUDA environment:
        self.gpu_ctx = gpu_ctx
        self.block_width = block_width
        self.block_height = 1
        
        # TODO: Where should the cl_queue come from?
        # For sure, the drifter and the ocean simulator should use 
        # the same queue...
        self.gpu_stream = gpu_stream
        if self.gpu_stream is None:
            self.gpu_stream = cuda.Stream()
                
        self.sensitivity = 1.0
         
        self.driftersHost = np.zeros((self.getNumDrifters() + 1, 2)).astype(np.float32, order='C')
        self.driftersDevice = Common.CUDAArray2D(self.gpu_stream, \
                                                 2, self.getNumDrifters()+1, 0, 0, \
                                                 self.driftersHost)
        
        self.drift_kernels = gpu_ctx.get_kernel("driftKernels.cu", \
                                                defines={'block_width': self.block_width, 'block_height': self.block_height})

        # Get CUDA functions and define data types for prepared_{async_}call()
        self.passiveDrifterKernel = self.drift_kernels.get_function("passiveDrifterKernel")
        self.passiveDrifterKernel.prepare("iifffiiPiPiPiPiiiiPif")
        self.enforceBoundaryConditionsKernel = self.drift_kernels.get_function("enforceBoundaryConditions")
        self.enforceBoundaryConditionsKernel.prepare("ffiiiPi")
        
        self.local_size = (self.block_width, self.block_height, 1)
        self.global_size = (\
                            int(np.ceil((self.getNumDrifters() + 2)/float(self.block_width))), \
                            1)
        
        # Initialize drifters:
        self.uniformly_distribute_drifters(initialization_cov_drifters=initialization_cov_drifters)
       
        #print "local_size: ", self.local_size
        #print "global_size: ", self.global_size
        #print "numDrifters + obs: ", self.numDrifters + 1
        # remember: shape = (y, x)
         
   
        
            
    def copy(self):
        """
        Makes an independent indentical copy of the current object
        """
    
        copyOfSelf = GPUDrifterCollection(self.gpu_ctx,
                                self.getNumDrifters(),
                                observation_variance = self.observation_variance,
                                boundaryConditions = self.boundaryConditions,
                                domain_size_x = self.domain_size_x, 
                                domain_size_y = self.domain_size_y,
                                gpu_stream = self.gpu_stream,
                                block_width = self.block_width)
        
        copyOfSelf.setDrifterPositions(self.getDrifterPositions())
        copyOfSelf.setObservationPosition(self.getObservationPosition())
        
        return copyOfSelf
    
    
    
    def setDrifterPositions(self, newDrifterPositions):
        ### Need to attache the observation to the newDrifterPositions, and then upload
        # to the GPU
        newPositionsAll = np.concatenate((newDrifterPositions, np.array([self.getObservationPosition()])), \
                                         axis=0)
        #print newPositionsAll
        self.driftersDevice.upload(self.gpu_stream, newPositionsAll)
    
    def setObservationPosition(self, newObservationPosition):
        newPositionsAll = np.concatenate((self.getDrifterPositions(), np.array([newObservationPosition])))
        self.driftersDevice.upload(self.gpu_stream, newPositionsAll)
        
    def setSensitivity(self, sensitivity):
        self.sensitivity = sensitivity
        
    def getDrifterPositions(self):
        allDrifters = self.driftersDevice.download(self.gpu_stream)
        return allDrifters[:-1, :]
    
    def getObservationPosition(self):
        allDrifters = self.driftersDevice.download(self.gpu_stream)
        return allDrifters[self.obs_index, :]
    
    def drift(self, eta, hu, hv, Hm, nx, ny, dx, dy, dt, \
              x_zero_ref, y_zero_ref):
        self.passiveDrifterKernel.prepared_async_call(self.global_size, self.local_size, self.gpu_stream, \
                                               nx, ny, dx, dy, dt, x_zero_ref, y_zero_ref, \
                                               eta.data.gpudata, eta.pitch, \
                                               hu.data.gpudata, hu.pitch, \
                                               hv.data.gpudata, hv.pitch, \
                                               Hm.data.gpudata, Hm.pitch, \
                                               np.int32(self.boundaryConditions.isPeriodicNorthSouth()), \
                                               np.int32(self.boundaryConditions.isPeriodicEastWest()), \
                                               np.int32(self.getNumDrifters()), \
                                               self.driftersDevice.data.gpudata, \
                                               self.driftersDevice.pitch, \
                                               np.float32(self.sensitivity))

    def setGPUStream(self, gpu_stream):
        self.gpu_stream = gpu_stream
        
    def cleanUp(self):
        if (self.driftersDevice is not None):
            self.driftersDevice.release()
        self.gpu_ctx = None
            
    def enforceBoundaryConditions(self):
        if self.boundaryConditions.isPeriodicNorthSouth or self.boundaryConditions.isPeriodicEastWest:
            self.enforceBoundaryConditionsKernel.prepared_async_call(self.global_size, self.local_size, self.gpu_stream, \
                                                        np.float32(self.domain_size_x), \
                                                        np.float32(self.domain_size_y), \
                                                        np.int32(self.boundaryConditions.isPeriodicNorthSouth()), \
                                                        np.int32(self.boundaryConditions.isPeriodicEastWest()), \
                                                        np.int32(self.numDrifters), \
                                                        self.driftersDevice.data.gpudata, \
                                                        self.driftersDevice.pitch)

