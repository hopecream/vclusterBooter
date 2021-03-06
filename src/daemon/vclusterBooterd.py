#!/usr/bin/env python

import os, sys
import pickle
import ConfigParser
import socket
import hashlib
import subprocess
import random
import time
import xml
from xml.dom import minidom

from lib.vmCommand import *
from lib.vmResult import *

#WARNING: This program is written in Python 2.6

class commandEngineError:
    def __init__(self, value, msg):
        self.value = value
        self.msg = msg
    def __str__(self):
        return "Command Engine Execute Error %d: %s" % (self.value, self.msg)

class commandEngine:


    def __init__(self, command = None, passwd=""):
        self._vclusterInstances = {}
        self._vclusterID = 0
        self._passwd = passwd

    # execute the command
    # return: [ret, msg]
    # ret is the return code
    # msg is the message that explains the code
    def run(self, command = None, sleepCycle = 0):

        self._command = command
        commandResult = vmCommandResult()

        if self._command is None:
            commandResult.retCode = 400
            commandResult.msg = "No command found"
            return commandResult
        

        if not self._authenticateUser(self._command):
            commandResult.retCode = 451
            commandResult.msg = "Authentication failed"
            return commandResult

        if(self._command.commID == 0):
            [retCode, msg, instance] = self._actionCreate(sleepCycle)
            commandResult.clusters.append(instance)
        elif(self._command.commID == 1):
            [retCode, msg] = self._actionDestroy()
        elif(self._command.commID == 2):
            [retCode, msg, list] = self._actionList()
            commandResult.clusters = list
        else:
            retCode = 401
            msg = "Undefined Command"

        commandResult.retCode = retCode
        commandResult.msg = msg
        return commandResult

    def _authenticateUser(self, command):
        authStr = command.timestamp + self._passwd
        hash = hashlib.sha1(authStr)
        authHash = hash.hexdigest()

        if authHash == command.passwd:
            return True
        else:
            return False

    def _actionCreate(self, sleepCycle):

        instance = vClusterInstance()

        networkNameMap = {}
        vnetFilename = ""
        networkNames = self._command.cluster.networks.keys()
        numberOfNames = len(networkNames)
        # First add virtual networks
        for networkIndex in range(numberOfNames):
            networkName = networkNames[networkIndex]
            networkSetting = self._command.cluster.networks[networkName]

            netInst = vNetInstance()
            if(networkSetting[0] == "private"):
                bridge = "eth1"

                networkTemplate = "NAME = \"%s\"\nTYPE = FIXED\nBRIDGE = %s\n"

                hash = hashlib.sha1(networkName + "-" + str(random.random()))
                UID = networkName + "-" + str(hash.hexdigest());
                networkNameMap[networkName] = UID
                
                networkTemplate = networkTemplate % (UID, bridge) 

                # add the LEASES = [IP=X.X.X.X] part
                for i in range(int(self._command.cluster.vmNR)):
                    lease = "LEASES = [IP=%s]\n"

                    # We need to do the check here
                    ipaddr = networkSetting[1]
                    addrParts = ipaddr.split(".");
                    addrParts[3] = str(int(addrParts[3]) + i)
                    ipaddr = ".".join(addrParts)

                    lease = lease % (ipaddr, )
                    networkTemplate += lease

                # write the template to the file
                hash = hashlib.sha1(str(random.random()))
                vnetFilename = "/tmp/" + hash.hexdigest() + ".vnet"

                fout = open(vnetFilename, "w")
                fout.write(networkTemplate)
                fout.close()

                # create the vnet
                try:
                    proc = subprocess.Popen(["onevnet", "create", vnetFilename, "-v"], stdout = subprocess.PIPE)
                    output = proc.communicate()[0]
                except:
                    raise commandEngineError(420, "Fail to create vnet using"\
                            " command onevnet and vnet template file %s" % vnetFilename)


                # delete the vnet template file after we create the vnet
                os.remove(vnetFilename)

                # sanity check
                outputs = output.strip("\n").split(" ")
                if outputs[0] != "ID:":
                    raise commandEngineError(422, "Fail to create vnet with ERROR message: %s" % (output, ))
                netInst.name = UID
                netInst.type = "private"
                netInst.mode = "FIXED"
                netInst.IP = networkSetting[1]
                netInst.id = outputs[1]
                instance.networks.append(netInst)
            else:
                netInst.name = "public-vnet"
                netInst.type = "public"
                netInst.mode = "RANGED"
                netInst.IP = networkSetting[1]
                netInst.id = -1
                instance.networks.append(netInst)
                # we do not need to care generating the vnetwork template for the public network
                continue

        # Second create virtual machines
        headerTemplate = """
NAME = "%s"
MEMORY = %s
OS = [
        bootloader = "/usr/bin/pygrub",
        root = "%s"]
"""
        #footerTemplate = "REQUIREMENTS = \"FREEMEMORY > %s\"\nRANK = \"- RUNNING_VMS\"\n"
        footerTemplate = "REQUIREMENTS = \"FREEMEMORY > %s\"\nRANK = \"FREEMEMORY\"\n"

        nicWithoutIPTemplate = "NIC = [NETWORK = \"%s\"]\n"
        nicWithIPTemplate = "NIC = [NETWORK = \"%s\", IP=%s]\n"

        diskTemplate = """
DISK = [
            source = "/srv/cloud/images/%s",
            target = "%s",
            readonly = "no",
            clone = "no"]
"""
        for vmIndex in range(int(self._command.cluster.vmNR)):
            vminst = vmInstance()

            template = self._command.cluster.vmTemplates[vmIndex]

            rootDevice = ""

            # process disk list
            diskList = ""
            for diskInfo in template.disks:
                diskDesc = diskTemplate % (diskInfo.diskName, diskInfo.diskTarget) 
                vminst.disks.append(diskInfo.diskName + ":" + diskInfo.diskTarget)

                if int(diskInfo.isRoot) != 0:
                    rootDevice = diskInfo.diskTarget
                diskList += diskDesc

            if rootDevice == "":
                return [501, "Cannot find the root device"]

            # process header and footer
            header = headerTemplate % (template.name, template.memory, rootDevice) 
            footer = footerTemplate % (str(int(template.memory) * 1024), )

            vminst.name = template.name
            vminst.memSize = int(template.memory)

            # process nics
            nicList = ""
            for nic in template.networkNames:
                (networkType, networkAddress) = self._command.cluster.networks[nic]

                if networkType == "public":
                    nicDesc = nicWithIPTemplate % ("public-vnet", networkAddress)
                    vminst.networkName.append("public-vnet")
                    vminst.ips.append(networkAddress)
                else:
                    nicDesc = nicWithoutIPTemplate % (networkNameMap[nic], )                     
                    vminst.networkName.append(networkNameMap[nic])
                    vminst.ips.append("N/A")
                nicList += nicDesc

            content = header + diskList + nicList + footer

            # write the template to the file
            hash = hashlib.sha1(str(random.random()))
            vmFilename = "/tmp/" + hash.hexdigest() + ".vm"

            fout = open(vmFilename, "w")
            fout.write(content)
            fout.close()

            # create the vm
            try:
                proc = subprocess.Popen(["onevm", "create", vmFilename, "-v"], stdout=subprocess.PIPE)
                output = proc.communicate()[0]
            except:
                raise commandEngineError(420, "Fail to create vm using"\
                        " command onevm and vm template file %s" % vmFilename)

            os.remove(vmFilename)

            # sanity check
            outputs = output.strip("\n").split(" ")
            if outputs[0] != "ID:":
                raise commandEngineError(421, "Fail to create vm with ERROR message: %s" % (output, ))
            vminst.id = int(outputs[1])
            vminst.status = "N/A"
            instance.vmInstances.append(vminst)

            time.sleep(sleepCycle)

        instance = self._fillInNetworkInfo(instance)
        instance = self._fillInStatusInfo(instance)

        # Ugly method
        instance.vmNR = self._command.cluster.vmNR
        instance.id = self._vclusterID;
        self._vclusterInstances[instance.id] = instance
        self._vclusterID += 1

        return [0, "successful", instance]

    def _actionDestroy(self):
        # vCluster to be destroyed is specified by its id
        # and stored in the self._command.commGeneralArgs

        if len(self._command.commGeneralArgs) != 1:
            return [402, "No vCluster ID is given"]

        # Sanity Check
        vclusterID = int(self._command.commGeneralArgs[0])
        
        if vclusterID not in self._vclusterInstances:
            return [403, "vCluster with ID %d is not existed" % (vclusterID, )]

        instance = self._vclusterInstances[vclusterID]
        
        # delete vm instances first
        for vmInst in instance.vmInstances:
            try:
                proc = subprocess.Popen(["onevm", "delete", str(vmInst.id)])
                proc.wait()
            except:
                raise commandEngineError(421, "Fail to delete vm with id %d" % (vmInst.id,))

        # delete networks
        for network in instance.networks:
            if network.type == "public":
                continue

            try:
                proc = subprocess.Popen(["onevnet", "delete", str(network.id)])
                proc.wait()
            except:
                raise commandEngineError(422, "Fail to delete network with id %d" % (network.id,))

        del self._vclusterInstances[vclusterID]

        return [0, "Successful"]

    def _actionList(self):
        
        for vcInstanceKey in self._vclusterInstances.keys():
           self._vclusterInstances[vcInstanceKey] = self._fillInStatusInfo(self._vclusterInstances[vcInstanceKey]) 

        return [0, "Successful", self._vclusterInstances.values()]

    def _fillInStatusInfo(self, vcInst):
        for vmInst in vcInst.vmInstances:
            try:
                proc = subprocess.Popen(["onevm", "show", str(vmInst.id), "-x"], stdout=subprocess.PIPE)
                vmInfoStr = proc.communicate()
            except:
                raise commandEngineError(424, "Fail to fill in the vm Status information")

            vmInfoXml = minidom.parseString(vmInfoStr[0])
            vmInst.status = self._extractVMStatusFromXml(vmInfoXml)

        return vcInst

    def _fillInNetworkInfo(self, vcInst):
        netInfoMap = {}

        for netInfo in vcInst.networks:
            if cmp(netInfo.type, "private") == 0:
                try:
                    proc = subprocess.Popen(["onevnet", "show", netInfo.name, "-x"], stdout=subprocess.PIPE)
                    netInfoStr = proc.communicate() 
                except:
                    raise commandEngineError(423, "Fail to fill in the network IP information")

                netInfoXml = minidom.parseString(netInfoStr[0])

                netInfoMap[netInfo.name] = netInfoXml

        for vmInst in vcInst.vmInstances:
            for i in range(len(vmInst.networkName)):
                if cmp(vmInst.ips[i], "N/A") == 0:
                   netInfoXml = netInfoMap[vmInst.networkName[i]] 
                   vmInst.ips[i] = self._extractIPFromXmlByVID(netInfoXml, vmInst.id)

        return vcInst

    def _extractIPFromXmlByVID(self, xmlroot, vid):
        leases = xmlroot.getElementsByTagName("LEASE")

        for lease in leases:
            VIDNode = lease.getElementsByTagName("VID")
            
            currVID = int(VIDNode[0].firstChild.data.strip('\n '))
            if currVID == int(vid):
                IPNode = lease.getElementsByTagName("IP")
                ipaddress = IPNode[0].firstChild.data.strip('\n ')
                return ipaddress

        return "N/A"

    def _extractVMStatusFromXml(self, xmlroot):
        try:
            lcmStateNode = xmlroot.getElementsByTagName("LCM_STATE")

            stateNumStr = lcmStateNode[0].firstChild.data.strip('\n ')
            stateNum = int(stateNumStr)

            if stateNum == 0:
                return "LCM_INIT"
            elif stateNum == 1:
                return "PROLOG"
            elif stateNum == 2:
                return "BOOT"
            elif stateNum == 3:
                return "RUNNING"
            elif stateNum == 4:
                return "MIGRATE"
            elif stateNum == 5:
                return "SAVE_STOP"
            elif stateNum == 6:
                return "SAVE_SUSPEND"
            elif stateNum == 7:
                return "SAVE_MIGRATE"
            elif stateNum == 8:
                return "PROLOG_MIGRATE"
            elif stateNum == 9:
                return "PROLOG_RESUME"
            elif stateNum == 10:
                return "EPILOG_STOP"
            elif stateNum == 11:
                return "EPILOG"
            elif stateNum == 12:
                return "SHUTDOWN"
            elif stateNum == 13:
                return "CANCEL"
            elif stateNum == 14:
                return "FAILURE"
            elif stateNum == 15:
                return "DELETE"
            elif stateNum == 16:
                return "UNKOWN"
            else:
                return "N/A"
        except:
            return "N/A"

class Listener:
    _bindAddress = None
    _bindPort = 57305

    _sock = None

    # Constructor
    def __init__(self, hostname = 'localhost', hostport = 57305, passwd=""):

        self._bindAddress = socket.gethostbyname(hostname)
        self._bindPort = hostport
        self._passwd = passwd

    # Running the Listner, which will listen for the incoming events
    def run(self, sleepCycle = 0):

        # Apply for a socket
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except socket.error, msg:
            self._sock = None
            print "Failed to create socket on %s:%s" % (self._bindAddress, self._bindPort)
            return -1

        # bind the address and port on the socket, and listen on it
        try:
            self._sock.bind((self._bindAddress, int(self._bindPort)))
            self._sock.listen(5)
        except socket.error, msg:
            self._sock.close()
            self._sock = None
            print "Failed to create bind and listen on the socket"
            return -1
        except:
            print "Failed to create bind and listen on the socket"
            return -1
        

        # Infinite loop for request handling
        # No need to use multithread here
        engine = commandEngine(passwd = self._passwd)
        while(1):
            conn, addr = self._sock.accept()
            print "Connection from ", addr

            rawData = self._readMessages(conn)
            if len(rawData) > 0:
                try:
                    vmCommand = pickle.loads(rawData)
                    result = engine.run(vmCommand, sleepCycle)
                except commandEngineError as e:
                    result = vmCommandResult()
                    result.code = 430
                    result.msg = str(e)
                    print result.msg
                except:
                    msg = "Unknown Error"
                    result = vmCommandResult()
                    result.code = 440
                    result.msg = msg
                    print result.msg
            else:
                result = vmCommandResult()
                result.ret = 404 
                result.msg = "ERROR 404, failed to extract the vmCommand packets"
                print result.msg

            outPacket = pickle.dumps(result, 2)
            self._sendMessage(conn, outPacket)
            conn.close()

    # read and extract messages
    def _readMessages(self, conn):
        rawDataSeg = conn.recv(4096)
        rawData = rawDataSeg

        while(len(rawData) == 4096):
            rawDataSeg = conn.recv(4096)
            rawData.extend(rawDataSeg)

        return rawData

    # send the execution result back
    def _sendMessage(self, conn, msg):
        conn.send(msg)

    def close(self):
        if(self._sock is not None):
            self._sock.close()
            self._sock = None


# handling events 
class vClusterBooterd:

    _hostname = None
    _hostport = None
    _listener = None

    def __init__(self, configFilename = 'vclusterBooterd.conf'):
        try:
            random.seed()

            # read the configs from the configuration file
            config = ConfigParser.ConfigParser()
            config.read(configFilename)
    
            self._hostname = config.get('server', 'hostname')
            self._hostport = config.get('server', 'port')
            self._vmCreateCycle = config.get('server', 'vmCreateCycle')
            self._passwd = config.get('server', 'passwd')

            print "Hostname is %s, port is %s" % (self._hostname, self._hostport)

            # Run the listener
            self._listener = Listener(self._hostname, self._hostport, self._passwd)
            ret = self._listener.run(int(self._vmCreateCycle))

        except ConfigParser.Error:
            print "Failed to read the configuration" 
        except IOError:
            if(self._listener is not None):
                self._listener.close()
            print "Failed to open the configuration File"
        except commandEngineError as e:
            if(self._listener is not None):
                self._listener.close()
            print e
        except KeyboardInterrupt as ki:
            if(self._listener is not None):
                self._listener.close()
            print ki
        except:
            if(self._listener is not None):
                self._listener.close()
            print "Unknown problem happens while reading configs"

if __name__ == '__main__':

    #TODO: We can add some command line config supporting here
    booter = vClusterBooterd()
