from django.db.models.functions import Lower
from rest_framework.reverse import reverse
from rest_framework.test import APITestCase

from course_discovery.apps.api.v1.tests.test_views.mixins import SerializationMixin
from course_discovery.apps.core.tests.factories import UserFactory, USER_PASSWORD
from course_discovery.apps.course_metadata.tests.factories import CourseFactory
from course_discovery.apps.course_metadata.models import Course


class CourseViewSetTests(SerializationMixin, APITestCase):
    def setUp(self):
        super(CourseViewSetTests, self).setUp()
        self.user = UserFactory(is_staff=True, is_superuser=True)
        self.client.login(username=self.user.username, password=USER_PASSWORD)
        self.course = CourseFactory()

    def test_get(self):
        """ Verify the endpoint returns the details for a single course. """
        url = reverse('api:v1:course-detail', kwargs={'key': self.course.key})

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, self.serialize_course(self.course))

    def test_list(self):
        """ Verify the endpoint returns a list of all catalogs. """
        url = reverse('api:v1:course-list')

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertListEqual(
            response.data['results'],
            self.serialize_course(Course.objects.all().order_by(Lower('key')), many=True)
        )
