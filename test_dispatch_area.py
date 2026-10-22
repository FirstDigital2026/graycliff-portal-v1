import unittest
from unittest.mock import patch
import operations_intake as o
class DispatchAreaTests(unittest.TestCase):
 def parse(self,city,routing):
  m={'subject':'Dead OTE NO LIGHT node 7B999E','body':{'content':'From: Dispatch <dispatch@graycliffent.com>\nAddress- 901 Fictional Rd '+city+' SC\n'+routing}}
  with patch.object(o.graph,'_candidate_document_texts',return_value=[]):return o.parse(m,[])
 def test_blackville(self):self.assertEqual(self.parse('Blackville','')['market'],'Columbia')
 def test_unknown_city_explicit_list(self):self.assertEqual(self.parse('Testville','To: NTP-Columbia <ntp-columbia@graycliffent.com>')['market'],'Columbia')
 def test_ambiguous_lists(self):self.assertEqual(self.parse('Testville','ntp-columbia@graycliffent.com ntp-florence@graycliffent.com')['market'],'')
 def test_wrong_domain(self):self.assertEqual(self.parse('Testville','ntp-columbia@graycliffent.com.example.invalid')['market'],'')
 def test_known_city_keeps_area(self):self.assertEqual(self.parse('Florence','ntp-columbia@graycliffent.com')['market'],'Florence')
if __name__=='__main__':unittest.main()
