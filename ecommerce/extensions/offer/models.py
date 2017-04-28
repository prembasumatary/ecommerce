from __future__ import unicode_literals

import re
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.utils.translation import ugettext_lazy as _
from oscar.apps.offer.abstract_models import AbstractBenefit, AbstractConditionalOffer, AbstractRange
from oscar.apps.offer import results
from requests.exceptions import ConnectionError, Timeout
from slumber.exceptions import SlumberBaseException
from threadlocals.threadlocals import get_current_request

from ecommerce.core.utils import get_cache_key, log_message_and_raise_validation_error


class Benefit(AbstractBenefit):
    VALID_BENEFIT_TYPES = [AbstractBenefit.PERCENTAGE, AbstractBenefit.FIXED]

    def save(self, *args, **kwargs):
        self.clean()
        super(Benefit, self).save(*args, **kwargs)  # pylint: disable=bad-super-call

    def clean(self):
        self.clean_type()
        self.clean_value()
        super(Benefit, self).clean()  # pylint: disable=bad-super-call

    def clean_type(self):
        if self.type not in self.VALID_BENEFIT_TYPES:
            log_message_and_raise_validation_error(
                'Failed to create Benefit. Unrecognised benefit type [{type}]'.format(type=self.type)
            )

    def clean_value(self):
        if self.value < 0:
            log_message_and_raise_validation_error(
                'Failed to create Benefit. Benefit value may not be a negative number.'
            )


class EnterpriseCustomerUserPercentageBenefit(Benefit):
    """
    An offer benefit that gives a percentage discount for enterprise learners.

    This custom benefit covers use cases having to do with establishing relationships between an
    Open edX user/learner and an enterprise customer (ref: http://github.com/edx/edx-enterprise).
    """
    enterprise_customer_uuid = models.UUIDField(
        help_text='UUID for an EnterpriseCustomer from the Enterprise Service.'
    )

    # The below code was bootstrapped from the example found at
    # http://django-oscar.readthedocs.io/en/releases-1.4/howto/how_to_create_a_custom_benefit.html

    # Note that we are adding a new UUID field above -- I am assuming that this cannot be a proxy
    # model because of this, since the intent of a proxy model is to include additional behavior on
    # top of an existing model class and in this case we also need to include additional state
    _description = _("%(value)s%% enterprise entitlement discount on %(range)s")

    @property
    def name(self):
        return self._description % {
            'value': self.value,
            'range': self.range.name
        }

    @property
    def description(self):
        return self._description % {
            'value': self.value,
            'range': self.range.name
        }

    class Meta:
        app_label = 'offer'
        verbose_name = _("Percentage enterprise entitlement discount benefit")
        verbose_name_plural = _("Percentage enterprise entitlement discount benefits")

    def apply(self, basket, condition, offer, discount_percent=None, max_total_discount=None):
        # imports are added here to avoid circular import
        from ecommerce.enterprise.utils import is_user_linked_to_enterprise_customer

        if not is_user_linked_to_enterprise_customer(self.enterprise_customer_uuid, basket.owner):
            return results.BasketDiscount(Decimal('0.0'))

        # TODO: Add check for data sharing consent

        if discount_percent is None:
            discount_percent = self.value

        discount_amount_available = max_total_discount

        line_tuples = self.get_applicable_lines(offer, basket)
        discount_percent = min(discount_percent, Decimal('100.0'))
        discount = Decimal('0.00')
        affected_items = 0
        max_affected_items = self._effective_max_affected_items()
        affected_lines = []
        for price, line in line_tuples:
            if affected_items >= max_affected_items:
                break
            if discount_amount_available == 0:
                break

            quantity_affected = min(line.quantity_without_discount,
                                    max_affected_items - affected_items)
            line_discount = self.round(discount_percent / Decimal('100.0') * price
                                       * int(quantity_affected))

            if discount_amount_available is not None:
                line_discount = min(line_discount, discount_amount_available)
                discount_amount_available -= line_discount

            line.discount(line, line_discount, quantity_affected)

            affected_lines.append((line, line_discount, quantity_affected))
            affected_items += quantity_affected
            discount += line_discount

        if discount > 0:
            condition.consume_items(offer, basket, affected_lines)
        return results.BasketDiscount(discount)


class ConditionalOffer(AbstractConditionalOffer):
    UPDATABLE_OFFER_FIELDS = ['email_domains', 'max_uses']
    email_domains = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        self.clean()
        super(ConditionalOffer, self).save(*args, **kwargs)   # pylint: disable=bad-super-call

    def clean(self):
        self.clean_email_domains()
        self.clean_max_global_applications()  # Our frontend uses the name max_uses instead of max_global_applications
        super(ConditionalOffer, self).clean()   # pylint: disable=bad-super-call

    def clean_email_domains(self):
        if self.email_domains == '':
            log_message_and_raise_validation_error(
                'Failed to create ConditionalOffer. ConditionalOffer email domains may not be an empty string.'
            )

        if self.email_domains:
            if not isinstance(self.email_domains, basestring):
                log_message_and_raise_validation_error(
                    'Failed to create ConditionalOffer. ConditionalOffer email domains must be of type string.'
                )

            email_domains_array = self.email_domains.split(',')

            if not email_domains_array[-1]:
                log_message_and_raise_validation_error(
                    'Failed to create ConditionalOffer. '
                    'Trailing comma for ConditionalOffer email domains is not allowed.'
                )

            for domain in email_domains_array:
                domain_parts = domain.split('.')
                error_message = 'Failed to create ConditionalOffer. ' \
                                'Email domain [{email_domain}] is invalid.'.format(email_domain=domain)

                # Conditions being tested:
                # - double hyphen not allowed
                # - must contain at least one dot
                # - top level domain must be at least two characters long
                # - hyphens are not allowed in top level domain
                # - numbers are not allowed in top level domain
                if any(['--' in domain,
                        len(domain_parts) < 2,
                        len(domain_parts[-1]) < 2,
                        re.findall(r'[-0-9]', domain_parts[-1])]):
                    log_message_and_raise_validation_error(error_message)

                for domain_part in domain_parts:
                    # - non of the domain levels can start or end with a hyphen before encoding
                    if domain_part.startswith('-') or domain_part.endswith('-'):
                        log_message_and_raise_validation_error(error_message)

                    # - all encoded domain levels must match given regex expression
                    if not re.match(r'^([a-z0-9-]+)$', domain_part.encode('idna')):
                        log_message_and_raise_validation_error(error_message)

    def clean_max_global_applications(self):
        if self.max_global_applications is not None:
            if self.max_global_applications < 1 or not isinstance(self.max_global_applications, (int, long)):
                log_message_and_raise_validation_error(
                    'Failed to create ConditionalOffer. max_global_applications field must be a positive number.'
                )

    def is_email_valid(self, email):
        """
        Check if the email is within the email_domains if email_domains are set,
        else return True. If there is a domain with a sub domain in the list of
        valid email domains then the user's email needs to match exactly the
        domain and sub domain. If there is only a domain (without sub domains) in
        the list of valid email domains then the user's domain needs to match
        regardless of the subdomain.

        Examples:

            1)
                email_domains value: 'example.com'
                valid user email domains:
                    'example.com', 'sub1.example.com', 'sub2.example.com' etc.
                invalid user email domains:
                    'other.com' etc.

            2)
                email_domains value: 'sub.example.com'
                valid user email domain:
                    'sub.example.com'
                invalid user email domains:
                    'sub1.example.com', 'example.com' etc.

        Args:
            email (str): Email of the user.

        Returns:
            True if the email is valid or when there are no valid email domains set,
            False otherwise.
        """
        if self.email_domains:
            for domain in self.email_domains.split(','):
                pattern = r'(?P<username>.+)@(?P<subdomain>\w+\.)*{domain}'.format(domain=domain)
                match = re.match(pattern, email)
                if match and match.group(0) == email:
                    return True
            return False
        return True

    def is_condition_satisfied(self, basket):
        """
        In addition to Oscar's check to see if the condition is satisfied,
        a check for if basket owners email domain is within the allowed email domains.
        """
        if not self.is_email_valid(basket.owner.email):
            return False
        return super(ConditionalOffer, self).is_condition_satisfied(basket)  # pylint: disable=bad-super-call


def validate_credit_seat_type(course_seat_types):
    if not isinstance(course_seat_types, basestring):
        log_message_and_raise_validation_error('Failed to create Range. Credit seat types must be of type string.')

    course_seat_types_list = course_seat_types.split(',')

    if len(course_seat_types_list) > 1 and 'credit' in course_seat_types_list:
        log_message_and_raise_validation_error(
            'Failed to create Range. Credit seat type cannot be paired with other seat types.'
        )

    if not set(course_seat_types_list).issubset(set(Range.ALLOWED_SEAT_TYPES)):
        log_message_and_raise_validation_error(
            'Failed to create Range. Not allowed course seat types {}. '
            'Allowed values for course seat types are {}.'.format(course_seat_types_list, Range.ALLOWED_SEAT_TYPES)
        )


class Range(AbstractRange):
    UPDATABLE_RANGE_FIELDS = [
        'catalog_query',
        'course_seat_types',
        'course_catalog',
        'enterprise_customer',
    ]
    ALLOWED_SEAT_TYPES = ['credit', 'professional', 'verified']
    catalog = models.ForeignKey(
        'catalogue.Catalog', blank=True, null=True, related_name='ranges', on_delete=models.CASCADE
    )
    catalog_query = models.TextField(blank=True, null=True)
    course_catalog = models.PositiveIntegerField(
        help_text=_('Course catalog id from the Catalog Service.'),
        null=True,
        blank=True
    )
    enterprise_customer = models.UUIDField(
        help_text=_('UUID for an EnterpriseCustomer from the Enterprise Service.'),
        null=True,
        blank=True,
    )
    course_seat_types = models.CharField(
        max_length=255,
        validators=[validate_credit_seat_type],
        blank=True,
        null=True
    )

    def save(self, *args, **kwargs):
        self.clean()
        super(Range, self).save(*args, **kwargs)  # pylint: disable=bad-super-call

    def clean(self):
        """ Validation for model fields. """
        if self.catalog and (self.course_catalog or self.catalog_query or self.course_seat_types):
            log_message_and_raise_validation_error(
                'Failed to create Range. Catalog and dynamic catalog fields may not be set in the same range.'
            )

        error_message = 'Failed to create Range. Either catalog_query or course_catalog must be given but not both ' \
                        'and course_seat_types fields must be set.'

        if self.catalog_query and self.course_catalog:
            log_message_and_raise_validation_error(error_message)
        elif (self.catalog_query or self.course_catalog) and not self.course_seat_types:
            log_message_and_raise_validation_error(error_message)
        elif self.course_seat_types and not (self.catalog_query or self.course_catalog):
            log_message_and_raise_validation_error(error_message)

        if self.course_seat_types:
            validate_credit_seat_type(self.course_seat_types)

    def run_catalog_query(self, product):
        """
        Retrieve the results from running the query contained in catalog_query field.
        """
        request = get_current_request()
        partner_code = request.site.siteconfiguration.partner.short_code
        cache_key = get_cache_key(
            site_domain=request.site.domain,
            partner_code=partner_code,
            resource='course_runs.contains',
            course_id=product.course_id,
            query=self.catalog_query
        )
        response = cache.get(cache_key)
        if not response:  # pragma: no cover
            try:
                response = request.site.siteconfiguration.course_catalog_api_client.course_runs.contains.get(
                    query=self.catalog_query,
                    course_run_ids=product.course_id,
                    partner=partner_code
                )
                cache.set(cache_key, response, settings.COURSES_API_CACHE_TIMEOUT)
            except:  # pylint: disable=bare-except
                raise Exception('Could not contact Course Catalog Service.')

        return response

    def catalog_contains_product(self, product):
        """
        Retrieve the results from using the catalog contains endpoint for
        catalog service for the catalog id contained in field "course_catalog".
        """
        request = get_current_request()
        partner_code = request.site.siteconfiguration.partner.short_code
        cache_key = get_cache_key(
            site_domain=request.site.domain,
            partner_code=partner_code,
            resource='catalogs.contains',
            course_id=product.course_id,
            catalog_id=self.course_catalog
        )
        response = cache.get(cache_key)
        if not response:
            course_catalog_api_client = request.site.siteconfiguration.course_catalog_api_client
            try:
                # GET: /api/v1/catalogs/{catalog_id}/contains?course_run_id={course_run_ids}
                response = course_catalog_api_client.catalogs(self.course_catalog).contains.get(
                    course_run_id=product.course_id
                )
                cache.set(cache_key, response, settings.COURSES_API_CACHE_TIMEOUT)
            except (ConnectionError, SlumberBaseException, Timeout):
                raise Exception('Unable to connect to Course Catalog service for catalog contains endpoint.')

        return response

    def contains_product(self, product):
        """
        Assert if the range contains the product.
        """
        # course_catalog is associated with course_seat_types.
        if self.course_catalog and self.course_seat_types:
            # Product certificate type should belongs to range seat types.
            if product.attr.certificate_type.lower() in self.course_seat_types:  # pylint: disable=unsupported-membership-test
                response = self.catalog_contains_product(product)
                # Range can have a catalog query and 'regular' products in it,
                # therefor an OR is used to check for both possibilities.
                return ((response['courses'][product.course_id]) or
                        super(Range, self).contains_product(product))  # pylint: disable=bad-super-call
        elif self.catalog_query and self.course_seat_types:
            if product.attr.certificate_type.lower() in self.course_seat_types:  # pylint: disable=unsupported-membership-test
                response = self.run_catalog_query(product)
                # Range can have a catalog query and 'regular' products in it,
                # therefor an OR is used to check for both possibilities.
                return ((response['course_runs'][product.course_id]) or
                        super(Range, self).contains_product(product))  # pylint: disable=bad-super-call
        elif self.catalog:
            return (
                product.id in self.catalog.stock_records.values_list('product', flat=True) or
                super(Range, self).contains_product(product)  # pylint: disable=bad-super-call
            )
        return super(Range, self).contains_product(product)  # pylint: disable=bad-super-call

    contains = contains_product

    def num_products(self):
        return len(self.all_products())

    def all_products(self):
        if (self.catalog_query or self.course_catalog) and self.course_seat_types:
            # Backbone calls the Voucher Offers API endpoint which gets the products from the Course Catalog Service
            return []
        if self.catalog:
            catalog_products = [record.product for record in self.catalog.stock_records.all()]
            return catalog_products + list(super(Range, self).all_products())  # pylint: disable=bad-super-call
        return super(Range, self).all_products()  # pylint: disable=bad-super-call


from oscar.apps.offer.models import *  # noqa isort:skip pylint: disable=wildcard-import,unused-wildcard-import,wrong-import-position,wrong-import-order,ungrouped-imports
