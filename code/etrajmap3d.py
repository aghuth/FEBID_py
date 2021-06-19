import pyvista as pv
import numpy as np
from math import *
import random as rnd
import sys
from tqdm import tqdm
import multiprocessing
import logging


import os

class ETrajMap3d(object):

    def __init__(self):
        self.DE = None # will hold accumulated deposited energy for each voxel
        self.state = None # wild hold states of voxels after read_vtk()
        self.grid = None # will hold uniform grid after read_vtk()
        self.nx, self.ny, self.nz = 0, 0, 0 # number of cells; will be set after read_vtk()
        self.e = 72 # fitting parameter related to energy required to initiate a SE cascade, material specific, eV
        self.lambda_escape = 3.5 # mean free escape path, material specific, nm
        self.x0, self.y0, self.z0 = 0, 0, 0  # origin of 3d grid
        rnd.seed()

    def read_vtk(self, fname):
        '''Read vtk file with 3d voxel data.
           fname: name of vtk file.
           Creates uniform grid and sets empty(=0), surface(=1) and volume(=2) state data.
        '''
        self.grid = pv.read(fname)
        nx, ny, nz = self.grid.dimensions # is 1 larger than number of cells in each direction
        self.nx, self.ny, self.nz = nx - 1, ny - 1, nz - 1
        self.cell_dim, self.cell_dim, self.cell_dim = self.grid.spacing
        self.DE = np.zeros((self.nx, self.ny, self.nz))
        self.state = np.reshape(self.grid.cell_arrays['state'], (self.nx, self.ny, self.nz), order='F')
        self.x0, self.y0, self.z0 = self.grid.origin

    def get_structure(self, structure, surface, cell_dim, x=0, y=0):
        self.grid = structure
        self.state = structure
        self.surface = surface
        self.cell_dim = cell_dim # absolute dimension of a cell, nm
        self.nz, self.ny, self.nx = np.asarray(self.grid.shape)- 1 # simulation chamber dimensions
        self.zdim_abs, self.ydim_abs, self.xdim_abs = [x*self.cell_dim for x in [self.nz, self.ny, self.nx]]
        self.DE = np.zeros((self.nz+1, self.ny+1, self.nx+1)) # array for storing of deposited energies
        self.flux = np.zeros((self.nz+1, self.ny+1, self.nx+1)) # array for storing SE fluxes
        self.dn = floor(self.lambda_escape * 3 / self.cell_dim) # number of cells an SE can intersect
        self.x = x
        self.y = y
        self.trajectories = [] # holds all trajectories mapped to 3d structure
        self.se_traj = []


    def __find_zshift(self, x, y):
        '''Finds and returns z-position where beam at (x, y) hits 3d structure.'''
        i, j = int((x - self.x0)/self.cell_dim), int((y - self.y0)/self.cell_dim) # converting absolute coordinates to array-coordinates
        for k in range(self.nz - 1, 0, -1): # proceeding from up to down
            if self.state[k,j,i] > self.state[self.nz-1, self.ny-1, self.nx-1]:# == 2 or self.state[i,j,k] == 1:
                return self.z0 + k*self.cell_dim # finishing on the first incident

    def __triple(self, p): # need always 'left-most' indices
        return  int(floor((p[0] - self.z0)/self.cell_dim)), int(floor((p[1] - self.y0)/self.cell_dim)), int(floor((p[2] - self.x0)/self.cell_dim))

    def __crossings(self, i, j, k, istp, jstp, kstp, p0, vd):
        if vd[0] == 0: # segment is perpendicular to z
            t0 = sys.float_info.max
        else:
            d = self.z0 + i*self.cell_dim # position of bottom wall of voxel
            if istp == 1: # right wall of voxel -> add cell_dim to d
                d += self.cell_dim
            t0 = (d - p0[0])/vd[0]
        if vd[1] == 0: # segment is perpendicular to y
            t1 = sys.float_info.max
        else:
            d = self.y0 + j*self.cell_dim # position of front wall of voxel
            if jstp == 1: # back wall of voxel -> add cell_dim to d
                d += self.cell_dim
            t1 = (d - p0[1])/vd[1]
        if vd[2] == 0: # segment is perpendicular to x
            t2 = sys.float_info.max
        else:
            d = self.x0 + k*self.cell_dim # position of left wall of voxel
            if kstp == 1: # top wall of voxel -> add cell_dim to d
                d += self.cell_dim
            t2 = (d - p0[2])/vd[2]
        return (t0, t1, t2)

    def __sign(self, x):
        if x < 0.0:
            return -1
        elif x > 0.0:
            return 1
        else:
            return 0


    def __get_indices(self, z=0, y=0, x=0, cell_dim=0.0001):
        """
        Gets indices of a cell in an array according to its position in the space

        :param x: X-coordinate
        :param y: Y-coordinate
        :param z: Z-coordinate
        :param cell_dim: dimension of a cell
        :return: i(z), j(y), k(x)
        """
        if cell_dim == 0.0001: cell_dim = self.cell_dim
        return int(z/cell_dim), int(y/cell_dim), int(x/cell_dim)


    def __check_boundaries(self, z=0, y=0, x=0):
        """
        Checks is the given (z,y,x) position is inside the simulation chamber

        :param z:
        :param y:
        :param x:
        :return:
        """
        if 0 <= x < self.xdim_abs:
            if 0 <= y < self.ydim_abs:
                if 0 <= z < self.zdim_abs:
                    return True
        return False


    # TODO: maybe __follow_segment can be written in cpython?
    def __follow_segment(self, traj, p1, p2, dE):
        """
           Map line segment between points p1 and p2 onto real 3D structure and calculate energy deposited
           in each volume or surface voxel along the mapped segment.

           traj: list of segments as result of mapping of original segment
           dE: list of energy deposited in each element of traj

        :param traj: resulting trajectory
        :param p1: first point
        :param p2: next point
        :param dE:
        :return:
        """
        """
        Algorithm:
            1. Take the endpoints of the line segment and the energy loss associated with this line segment as input
            2. Calculate where the first point of the line segments is positioned within the simulation volume -> voxel
            3. Calculate where the line segment spanned by the fist and second point crosses the voxel surface of the voxel
            4. This point is stored for later use as it will become the first point for the next loop
            5. Check whether the type of voxel for which two segment endpoints are now known is of volume or surface type (i.e. filled)
            6. If it is filled the energy deposited in the voxel is calculated using the length of the segment
            7. The original length of the starting segment is reduced by the length of the segment inside the voxel
            8. If the remaining length runs below 0, the function returns
            9. If in the loop the mapped segments leave the simulation volume, the function returns with cont = False
            10. With the voxel intersection point 2 now becoming the first point the loop is repeated
        """
        vd = p2 - p1 # vector between two points
        L0 = sqrt(vd.dot(vd)) # length of segment
        L = L0 # L will be reduced from iteration to iteration until length <= 0, then leave
        # indicators for orientation of segment in space
        istp, jstp, kstp = self.__sign(vd[0]), self.__sign(vd[1]), self.__sign(vd[2]) # steps in the array
        p0 = p1 # p0 stores first endpoint of segment
        pr = np.copy(p2)  # will be changed in program and returned as point where next segment has to attach to
        i, j, k = self.__triple(p0)  # index triple of voxel where p0 lies within 3D structure
        # auxiliary real numbers to find where segment crosses surface of voxel
        t0, t1, t2 = self.__crossings(i, j, k, istp, jstp, kstp, p0, vd)
        t = 0.0
        cont = True # monitors when mapped segment leaves simulation volume -> cont = False
        while True:
            di = dj = dk = 0
            traj.append(p0) # appending the first point
            # following if-sequence and __crossing from fast method to find intersection of line segment
            # with box surface; taken from book about graphics programming in C and translated to python
            if t2 < t1:
                if t2 < t0:
                    t += t2
                    k += kstp
                    dk = kstp
                else:
                    t += t0
                    i += istp
                    di = istp
            else:
                if t1 < t0:
                    t += t1
                    j += jstp
                    dj = jstp
                else:
                    t += t0
                    i += istp
                    di = istp
            # t here is basically the smallest ratio between coordinate components and the vector
            ps = p1 + t*vd # actual point where box surface is crossed
            dp = ps - p0
            dL = sqrt(dp.dot(dp)) # length of segment from start point to crossing point
            if i < 0 or i >= self.nz or j < 0 or j >= self.ny or k < 0 or k >= self.nx: # checking if we are out of the array
                cont = False # segment runs out of simulation box
                traj.append(ps)
                break
            # calculate energy deposited in mapped segment if segment goes through surface or volume voxel
            state_old = self.state[i-di,j-dj,k-dk]
            if state_old == -2 or state_old == -1:
                L -= dL
                if L < 0.0:
                    if i - di >= 0:
                        dp = pr - traj[-1]
                        de = sqrt(dp.dot(dp))/L0*dE
                        self.DE[i-di,j-dj,k-dk] += de # depositing energy in the corresponding cell
                        if de != 0:
                            # TODO: when cell size is reduced, deposited energy per cell may go lower than activation energy
                            #  eliminating any SE emission
                            #  SE emission has to be evaluated per step of a trajectory segment
                            self.generate_se(de, i-di,j-dj,k-dk, pr)

                    traj.append(pr)
                    break
                traj.append(ps)
                if i - di >= 0:
                    self.DE[i-di,j-dj,k-dk] += dE*dL/L0
                    if dE*dL/L0 != 0:
                        self.generate_se(self.DE[i-di,j-dj,k-dk], i-di,j-dj,k-dk, ps)
            else:
                pr += ps - traj[-1]
                traj.append(ps)
            p0 = ps  # box crossing point becomes new start point
            # calculate new auxillary numbers for next crossing point calculation
            t0, t1, t2 = self.__crossings(i, j, k, istp, jstp, kstp, p0, vd)
        return pr, cont

    def generate_se(self, de, i, j, k, pr):
        """
        Generates SEs depending on the deposited energy and checks it they reach surface

        :param de: deposited energy
        :param i: z array position
        :param j: y array position
        :param k: x array position
        :param pr: initial scattering point
        :return:
        """
        if self.surface[i - self.dn:i + self.dn + 1, j - self.dn:j + self.dn + 1, k - self.dn:k + self.dn + 1].any(): # check if SE can reach surface
            n_se = de / self.e  # number of generated SEs, usually ~0.1
            # for g in range(int(n_se)):
            alpha = rnd.uniform(-1, 1) * 2 * pi
            gamma = rnd.uniform(-1, 1) * 2 * pi
            length = self.lambda_escape * 2
            # s1, s2, s3 = int((pr[0]+length*cos(gamma))/self.cell_dim), int((pr[1]+length*sin(alpha))/self.cell_dim), int((pr[2]+length*cos(alpha))/self.cell_dim)
            s1, s2, s3 = (pr[0] + length * cos(gamma)), (pr[1] + length * sin(alpha)), (pr[2] + length * cos(alpha))
            self.se_traj.append([pr, np.array([s1, s2, s3])]) # save SE trajectory for plotting
            try: # using try-except clause instead of checking boundaries, because in case of escape electron is just abandoned
                if self.grid[self.__get_indices(s1, s2, s3)] < 1:  # if electron escapes solid
                    dz, dy, dx = (pr - (s1, s2, s3))/3
                    for n in range(3):  # # track which surface cell catches it
                        i,j,k = self.__get_indices(s1, s2, s3)
                        if self.surface[i,j,k] == True:
                            self.flux[i,j,k] += n_se
                            break
                        else:
                            s1 += dz
                            s2 += dy
                            s3 += dx
            except Exception as e: # printing out the actual exception just in case
                logging.exception('Caught an Error:')
                print("Skipping an electron")

    def __setup_trajectory(self, points, energies):
        '''Setup trajectory from MC simulation data for further computation.
           points: list of (x, y, z) points of trajectory from MC simulation
           energies: list of residual energies of electron at points of trajectory in keV
           xb, yb: lateral position of beam to hit the 3d structure.
           Returns lists of points and deposited energies (in eV) with corrected coordinates for 3d structure mapping.
        '''
        """
        Gauss distribution is now handled before PE trajectories simulation, where they are mapped according to the real structure
        Z-positions are also taken into account in that step
        """
        pnp = np.array(points[0:len(points)-1]) # to get easy access to x, y, z coordinates of points
        dE = np.asarray(energies)
        dE -= np.roll(dE, -1)
        dE.resize(len(dE)-1, refcheck=False) # last element is discarded
        dE *=1000
        return pnp, dE


    def map_trajectory(self, passes, n=8):
        '''Do actual mapping of trajectory and energy loss onto 3d structure.
           points: list of (x, y, z) points of trajectory from MC simulation
           energies: list of deposited energies at the (x, y, z) points from MC simulation
           xb, yb: lateral position of beam to hit the 3d structure.
           Adds calculated trajectory in 3d structure to self.trajectories and updates
           entries in self.DE regarding accumulated deposited energy in each voxel.
        '''
        print("\nDepositing energy and generating SEs")
        pas = list(np.array_split(np.asarray(passes), n))
        with multiprocessing.Pool(n) as pool:
            results = pool.map(self.map_follow, pas)
        for p in results:
            self.flux += p[0]
            self.DE += p[1]
            self.se_traj += p[2]
        # print("Done")
        a=0

    def map_follow(self, passes):
        # for one_pass in tqdm(passes):
        for one_pass in passes:
            pts, dEs = self.__setup_trajectory(one_pass[0][1:], one_pass[1][1:])  # Adding distance from the beam origin to surface to all the points (shifting trajectories down) and getting energy losses
            # self.__mesh_traj(pts)
            p1 = pts[0]
            traj = []
            for i in range(len(pts) - 1):
                p2 = p1 + (pts[i + 1] - pts[i])  # set p1 and p2 as endpoints of current segment
                p1, cont = self.__follow_segment(traj, p1, p2, dEs[i])
                if not cont:  # if endpoint leaves simulation volume break
                    break
            # self.trajectories.append(traj)
        return self.flux, self.DE, self.se_traj # has to be returned, as every process has its own copy of the whole class and thus does not write to the original

    def __mesh_traj(self, traj):
        lines = np.asarray(traj)
        m = pv.PolyData()
        m.points = lines
        cells = np.full((len(lines) - 1, 3), 2, dtype=np.int_)
        cells[:, 1] = np.arange(0, len(lines) - 1, dtype=np.int_)
        cells[:, 2] = np.arange(1, len(lines), dtype=np.int_)
        m.lines = cells
        line = m.tube(radius=2)
        self.tr_plot.add_mesh(line, color='red')