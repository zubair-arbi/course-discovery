import logging
import re

from django.db import transaction
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext as _
from django_filters.rest_framework import DjangoFilterBackend
from edx_rest_api_client.client import OAuthAPIClient
from rest_framework import filters as rest_framework_filters
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from course_discovery.apps.api import filters, serializers
from course_discovery.apps.api.pagination import ProxiedPagination
from course_discovery.apps.api.permissions import WriteOnlyByStaffUser
from course_discovery.apps.api.utils import get_query_param
from course_discovery.apps.course_metadata.choices import CourseRunStatus
from course_discovery.apps.course_metadata.constants import COURSE_ID_REGEX, COURSE_UUID_REGEX
from course_discovery.apps.course_metadata.models import Course, CourseEntitlement, CourseRun, Organization, SeatType

logger = logging.getLogger(__name__)


class EcommerceAPIClientException(Exception):
    pass


# pylint: disable=no-member
class CourseViewSet(viewsets.ModelViewSet):
    """ Course resource. """

    # Check if there's available syntax for ordering by join children elements
    filter_backends = (DjangoFilterBackend, rest_framework_filters.OrderingFilter)
    filter_class = filters.CourseFilter
    lookup_field = 'key'
    lookup_value_regex = COURSE_ID_REGEX + '|' + COURSE_UUID_REGEX
    permission_classes = (IsAuthenticated, WriteOnlyByStaffUser,)
    serializer_class = serializers.CourseWithProgramsSerializer

    course_key_regex = re.compile(COURSE_ID_REGEX)
    course_uuid_regex = re.compile(COURSE_UUID_REGEX)

    # Explicitly support PageNumberPagination and LimitOffsetPagination. Future
    # versions of this API should only support the system default, PageNumberPagination.
    pagination_class = ProxiedPagination

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())

        key = self.kwargs['key']

        if self.course_key_regex.match(key):
            filter_key = 'key'
        elif self.course_uuid_regex.match(key):
            filter_key = 'uuid'

        filter_kwargs = {filter_key: key}
        obj = get_object_or_404(queryset, **filter_kwargs)

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj

    def get_queryset(self):
        partner = self.request.site.partner
        q = self.request.query_params.get('q')

        if q:
            queryset = Course.search(q)
            queryset = self.get_serializer_class().prefetch_queryset(queryset=queryset, partner=partner)
        else:
            if get_query_param(self.request, 'include_hidden_course_runs'):
                course_runs = CourseRun.objects.filter(course__partner=partner)
            else:
                course_runs = CourseRun.objects.filter(course__partner=partner).exclude(hidden=True)

            if get_query_param(self.request, 'marketable_course_runs_only'):
                course_runs = course_runs.marketable().active()

            if get_query_param(self.request, 'marketable_enrollable_course_runs_with_archived'):
                course_runs = course_runs.marketable().enrollable()

            if get_query_param(self.request, 'published_course_runs_only'):
                course_runs = course_runs.filter(status=CourseRunStatus.Published)

            queryset = self.get_serializer_class().prefetch_queryset(
                queryset=self.queryset,
                course_runs=course_runs,
                partner=partner
            )

        return queryset.order_by(Lower('key'))

    def get_serializer_context(self, *args, **kwargs):
        context = super().get_serializer_context(*args, **kwargs)
        query_params = ['exclude_utm', 'include_deleted_programs']

        for query_param in query_params:
            context[query_param] = get_query_param(self.request, query_param)

        return context

    def get_course_key(self, data):
        return '{org}+{number}'.format(org=data['org'], number=data['number'])

    def create(self, request, *args, **kwargs):
        """
        Create a Course, Course Entitlement, and Entitlement Product in E-commerce.
        """
        course_creation_fields = {
            'title': request.data.get('title'),
            'number': request.data.get('number'),
            'org': request.data.get('org'),
            'mode': request.data.get('mode'),
        }
        missing_values = [k for k, v in course_creation_fields.items() if v is None]
        error_message = ''
        if missing_values:
            error_message += ''.join([_('Missing value for: [{name}]. ').format(name=name) for name in missing_values])
        if not Organization.objects.filter(key=course_creation_fields['org']).exists():
            error_message += _('Organization does not exist. ')
        if not SeatType.objects.filter(slug=course_creation_fields['mode']).exists():
            error_message += _('Entitlement Track does not exist. ')
        if error_message:
            return Response((_('Incorrect data sent. ') + error_message).strip(), status=status.HTTP_400_BAD_REQUEST)
        else:
            partner = request.site.partner
            course_creation_fields['partner'] = partner.id
            course_creation_fields['key'] = self.get_course_key(course_creation_fields)
            serializer = self.get_serializer(data=course_creation_fields)
            serializer.is_valid(raise_exception=True)
            try:
                with transaction.atomic():
                    course = serializer.save()
                    price = request.data.get('price', 0.00)
                    mode = SeatType.objects.get(slug=course_creation_fields['mode'])
                    entitlement = CourseEntitlement.objects.create(
                        course=course,
                        mode=mode,
                        partner=partner,
                        price=price,
                    )

                    api_client = OAuthAPIClient(partner.lms_url, partner.oidc_key, partner.oidc_secret)
                    ecom_entitlement_data = {
                        'product_class': 'Course Entitlement',
                        'title': course.title,
                        'price': price,
                        'certificate_type': course_creation_fields['mode'],
                        'uuid': str(course.uuid),
                    }
                    ecom_response = api_client.post(
                        partner.ecommerce_api_url + 'products/', data=ecom_entitlement_data
                    )
                    if ecom_response.status_code == 201:
                        stockrecord = ecom_response.json()['stockrecords'][0]
                        entitlement.sku = stockrecord['partner_sku']
                        entitlement.save()
                    else:
                        raise EcommerceAPIClientException(ecom_response.text)
            except EcommerceAPIClientException as e:
                logger.exception(
                    _('The following error occurred while creating the Course Entitlement in E-commerce: '
                      '{ecom_error}').format(ecom_error=e)
                )
                return Response(_('Failed to add course data due to a failure in product creation.'),
                                status=status.HTTP_400_BAD_REQUEST)
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    _('An error occurred while creating the Course [%s].'), serializer.validated_data['title']
                )
                return Response(_('Failed to add course data.'), status=status.HTTP_400_BAD_REQUEST)
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def list(self, request, *args, **kwargs):
        """ List all courses.
         ---
        parameters:
            - name: exclude_utm
              description: Exclude UTM parameters from marketing URLs.
              required: false
              type: integer
              paramType: query
              multiple: false
            - name: include_deleted_programs
              description: Will include deleted programs in the associated programs array
              required: false
              type: integer
              paramType: query
              multiple: false
            - name: keys
              description: Filter by keys (comma-separated list)
              required: false
              type: string
              paramType: query
              multiple: false
            - name: include_hidden_course_runs
              description: Include course runs that are hidden in the response.
              required: false
              type: integer
              paramType: query
              mulitple: false
            - name: marketable_course_runs_only
              description: Restrict returned course runs to those that are published, have seats,
                and are enrollable or will be enrollable in the future
              required: false
              type: integer
              paramType: query
              mulitple: false
            - name: marketable_enrollable_course_runs_with_archived
              description: Restrict returned course runs to those that are published, have seats,
                and can be enrolled in now. Includes archived courses.
              required: false
              type: integer
              paramType: query
              mulitple: false
            - name: published_course_runs_only
              description: Filter course runs by published ones only
              required: false
              type: integer
              paramType: query
              mulitple: false
            - name: q
              description: Elasticsearch querystring query. This filter takes precedence over other filters.
              required: false
              type: string
              paramType: query
              multiple: false
        """
        return super(CourseViewSet, self).list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """ Retrieve details for a course. """
        return super(CourseViewSet, self).retrieve(request, *args, **kwargs)
