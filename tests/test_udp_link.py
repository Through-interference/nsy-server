import unittest
from nsy_server.udp_link import encode,decode,Session

class DatagramTests(unittest.TestCase):
 def test_binary_roundtrip(self):
  data=bytes(range(256))*4
  self.assertEqual(decode(encode(b'D',data)),(b'D',data))
 def test_unknown_version_and_type_rejected(self):
  for data in (b'',b'garbage',b'SKZ1X',b'SKZ1Hpayload'):
   with self.assertRaises(ValueError):decode(data)
 def test_server_lease_only_configured_peer(self):
  session=Session('10.0.0.2',ttl=3)
  self.assertFalse(session.accept(('10.0.0.3',1234),0))
  self.assertTrue(session.accept(('10.0.0.2',1234),0))
  self.assertEqual(session.destination(2),('10.0.0.2',1234))
  self.assertIsNone(session.destination(4))
if __name__=='__main__':unittest.main()
