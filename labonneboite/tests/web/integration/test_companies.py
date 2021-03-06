# coding: utf8
from labonneboite.common.models import Office
from labonneboite.tests.test_base import DatabaseTest


class RouteTest(DatabaseTest):

    def create_example_office(self):
        # Create an office.
        self.office = Office(
            departement=u'75',
            siret=u'78548035101646',
            company_name=u'NICOLAS',
            headcount=u'03',
            city_code=u'75110',
            zipcode=u'75010',
            naf=u'7320Z',
            tel=u'0100000000',
            score=80,
            x=2.3488,
            y=48.8534,
        )
        self.office.save()

    def test_office_fields_and_properties_are_unicode(self):
        """
        Check if office fields are unicode
        """

        self.create_example_office()

        self.assertEqual(type(self.office.company_name), unicode)
        self.assertEqual(type(self.office.address_as_text), unicode)
        self.assertEqual(type(self.office.phone), unicode)
        self.assertEqual(type(self.office.google_url), unicode)

    def test_office_details_page(self):
        """
        Test the company details page.
        """

        self.create_example_office()

        rv = self.app.get('/%s/details' % self.office.siret)
        self.assertEqual(rv.status_code, 200)

        # The details page of an nonexistent company should raise a 404.
        rv = self.app.get('/7x5x8x3x1x1x46/details')
        self.assertEqual(rv.status_code, 404)

    def test_download_missing_siret(self):
        """
        Test the company PDF download
        """

        self.create_example_office()

        # normal behavior
        rv = self.app.get("/%s/download" % self.office.siret)
        self.assertEqual(rv.status_code, 200)

        # siret does not exist
        rv = self.app.get("/1234567890/download")
        self.assertEqual(rv.status_code, 404)
