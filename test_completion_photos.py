import base64, hashlib, unittest
from email import policy
from email.parser import BytesParser
import completion_photos as photos
import mail_automation as mail

class CompletionPhotos(unittest.TestCase):
    def setUp(self):
        self.event={'status':'Field complete','jobKey':'columbia:123'}
        self.draft={'eventId':'fictional-event','jobKey':'columbia:123','subject':'Re: Fictional job','to':['dispatch@example.com'],'cc':[],'parent':'<fictional@example.com>','references':['<fictional@example.com>'],'text':'Complete','html':'<p>Complete</p><hr>'}
    def package(self,raw=b'\xff\xd8fictional-jpeg'):
        return {'jobKey':'columbia:123','photos':[{'name':'Field-photo-1.jpg','content':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()}]}
    def attach(self,p):
        return photos.attach(self.draft,self.event,None,lambda *a:p,mail.ThreadReview)
    def test_real_attachment_and_original_thread(self):
        result=self.attach(self.package());mime=BytesParser(policy=policy.default).parsebytes(mail.mime_message(result));files=list(mime.iter_attachments())
        self.assertEqual(len(files),1);self.assertEqual(files[0].get_content_disposition(),'attachment');self.assertEqual(files[0].get_payload(decode=True),b'\xff\xd8fictional-jpeg');self.assertEqual(mime['In-Reply-To'],'<fictional@example.com>')
    def test_no_photos_is_allowed(self):
        self.assertEqual(self.attach({'jobKey':'columbia:123','photos':[]})['attachments'],[])
    def test_progress_does_not_request_completion_photos(self):
        self.event['status']='On site'
        result=photos.attach(self.draft,self.event,None,lambda *a:self.fail('Completion lookup during progress'),mail.ThreadReview)
        self.assertIs(result,self.draft)
    def test_wrong_job_is_held(self):
        p=self.package();p['jobKey']='florence:999'
        with self.assertRaises(mail.ThreadReview):self.attach(p)
    def test_corruption_is_held(self):
        p=self.package();p['photos'][0]['sha256']='0'*64
        with self.assertRaises(mail.ThreadReview):self.attach(p)
    def test_oversized_package_not_silently_truncated(self):
        with self.assertRaises(mail.ThreadReview):self.attach(self.package(b'\xff\xd8'+b'x'*1572864))
    def test_document_is_not_accepted_as_photo(self):
        with self.assertRaises(mail.ThreadReview):self.attach(self.package(b'%PDF-fictional'))
    def test_duplicate_package_is_held(self):
        p=self.package();p['photos']*=2
        with self.assertRaises(mail.ThreadReview):self.attach(p)
    def test_excess_photos_not_omitted(self):
        p=self.package();p['photos']*=21
        with self.assertRaises(mail.ThreadReview):self.attach(p)

if __name__=='__main__':unittest.main()
