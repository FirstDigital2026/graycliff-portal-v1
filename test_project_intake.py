import unittest
from unittest.mock import patch
import operations_intake as o

class ProjectIntakeTests(unittest.TestCase):
    def test_email_does_not_select_project_category(self):
        for subject in ['[PROJECT] Example expansion','Project: Example expansion','Multi-day project - Example expansion']:
            self.assertEqual(o.category_for_subject(subject),'Standard')
        self.assertEqual(o.category_for_message('Normal work order','Category: Project\nArea: Columbia'),'Standard')

    def test_existing_categories_and_ntp_project_id_are_unchanged(self):
        for subject,expected in [('No light at 100 Example Rd','Outage'),('CRQ 12345 low light repair','CRQ'),('Project ID 1234567 - normal work order','Standard'),('PO Number for NTP 1234 PRISM 1234567 Created/Revised','Standard')]:
            self.assertEqual(o.category_for_subject(subject),expected)

    def test_outage_subject_keeps_normal_classification(self):
        self.assertEqual(o.category_for_subject('[PROJECT] Permanent replacement after outage'),'Outage')

    def test_project_does_not_absorb_unrelated_small_job_at_same_name(self):
        class Store:
            def get_sheet(self,sid,force=False):
                return {'columns':[{'id':1,'title':'Task Name'},{'id':2,'title':'Portal Job Type'}],'rows':[{'id':7,'cells':[{'columnId':1,'value':'Example subdivision expansion'},{'columnId':2,'value':'Standard'}]}]} if sid==o.AREA['Florence'] else {'columns':[],'rows':[]}
        with self.assertRaisesRegex(ValueError,'separate'):
            o.locate(Store(),{'pid':'','wo':'','address':'Example subdivision expansion','city':'','type':'Project'})

    def test_same_project_matches_existing_record(self):
        class Store:
            def get_sheet(self,sid,force=False):
                return {'columns':[{'id':1,'title':'Task Name'},{'id':2,'title':'Portal Job Type'}],'rows':[{'id':7,'cells':[{'columnId':1,'value':'Example subdivision expansion'},{'columnId':2,'value':'Project'}]}]} if sid==o.AREA['Florence'] else {'columns':[],'rows':[]}
        found=o.locate(Store(),{'pid':'','wo':'','address':'Example subdivision expansion','city':'','type':'Project'})
        self.assertEqual(found[2]['id'],7)
        reply=o.locate(Store(),{'pid':'','wo':'','address':'Example subdivision expansion','city':'','type':'Standard'},linked=found)
        self.assertEqual(reply[2]['id'],7)

if __name__=='__main__':unittest.main()
