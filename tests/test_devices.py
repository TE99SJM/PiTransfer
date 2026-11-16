import copy
import json
import unittest
from unittest.mock import patch

import drive_helper


def disk(name='/dev/sda', tran='usb', mounted=None, fs='exfat'):
    return {"name":name,"type":"disk","tran":tran,"rm":False,"size":100000000,"fstype":None,"label":None,"serial":"SERIAL","uuid":None,"maj:min":"8:0","mountpoints":[None],"children":[{"name":name+'1',"type":"part","tran":None,"size":90000000,"fstype":fs,"label":"FILES","uuid":"UUID","maj:min":"8:1","mountpoints":[mounted]}]}


class DeviceTest(unittest.TestCase):
    def scan(self, data):
        with patch('drive_helper.command',return_value=json.dumps({'blockdevices':data})):
            return drive_helper.scan()

    def test_only_usb_devices(self):
        devices = self.scan([disk(),disk('/dev/mmcblk0','mmc','/'),disk('/dev/nvme0n1','nvme',None)])
        self.assertEqual(len(devices),1)
        self.assertTrue(devices[0]['usable'])

    def test_usb_boot_disk_and_mounted_disks_blocked(self):
        for mounted in ('/','/boot/firmware','/media/other'):
            self.assertFalse(self.scan([disk(mounted=mounted)])[0]['usable'])

    def test_mounted_sibling_blocks_entire_usb_disk(self):
        d = disk()
        sibling=copy.deepcopy(d['children'][0])
        sibling.update(name='/dev/sda2',**{'maj:min':'8:2','mountpoints':['/']})
        d['children'].append(sibling)
        self.assertTrue(all(not x['usable'] for x in self.scan([d])))

    def test_encrypted_filesystem_blocked(self):
        self.assertFalse(self.scan([disk(fs='crypto_LUKS')])[0]['usable'])

    def test_replaced_device_changes_identifier(self):
        d=disk();first=self.scan([d])[0]['id']
        d['children'][0]['uuid']='REPLACED'
        self.assertNotEqual(first,self.scan([d])[0]['id'])

    def test_helper_rejects_arguments(self):
        with patch('drive_helper.scan',return_value=[]):
            for action,token in [('shell','8:1'),('mount','../../etc'),('eject','--all')]:
                with self.assertRaises(ValueError):
                    drive_helper.perform(action,token)


if __name__ == '__main__':
    unittest.main()
