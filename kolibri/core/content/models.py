"""
These models are used in the databases of content that get imported from Studio.
Any fields added here (and not in base_models.py) are assumed to be locally
calculated cached fields. If a field is intended to be imported from a content
database generated by Studio, it should be added in base_models.py.


*DEVELOPER WARNING regarding updates to these models*

If you modify the schema here, it has implications for the content import pipeline
because we will need to calculate these values during content import (as they will
not be present in the content databases distributed by Studio).

In the case where new fields are added that do not need to be added to an export schema
the generate_schema command should be run like this:

    `kolibri manage generate_schema current`

This will just regenerate the current schema for SQLAlchemy, so that we can use SQLAlchemy
to calculate these fields if needed (this can frequently be more efficient than using the
Django ORM for these calculations).
"""
from __future__ import print_function

import os
import uuid
from gettext import gettext as _

from django.db import connection
from django.db import models
from django.db.models import F
from django.db.models import Min
from django.db.models import Q
from django.db.models import QuerySet
from django.utils.encoding import python_2_unicode_compatible
from le_utils.constants import content_kinds
from le_utils.constants import format_presets
from morango.models.fields import UUIDField
from mptt.managers import TreeManager
from mptt.querysets import TreeQuerySet

from .utils import paths
from kolibri.core.auth.models import Facility
from kolibri.core.auth.models import FacilityUser
from kolibri.core.content import base_models
from kolibri.core.content.errors import InvalidStorageFilenameError
from kolibri.core.content.utils.search import bitmask_fieldnames
from kolibri.core.content.utils.search import metadata_bitmasks
from kolibri.core.device.models import ContentCacheKey
from kolibri.core.fields import DateTimeTzField
from kolibri.core.fields import JSONField
from kolibri.core.mixins import FilterByUUIDQuerysetMixin
from kolibri.utils.data import ChoicesEnum
from kolibri.utils.time_utils import local_now


PRESET_LOOKUP = dict(format_presets.choices)


@python_2_unicode_compatible
class ContentTag(base_models.ContentTag):
    def __str__(self):
        return self.tag_name


class ContentNodeQueryset(TreeQuerySet, FilterByUUIDQuerysetMixin):
    def dedupe_by_content_id(self, use_distinct=True):
        # Cannot use distinct if queryset is also going to use annotate,
        # so optional use_distinct flag can be used to fallback to a subquery
        # remove duplicate content nodes based on content_id
        if connection.vendor == "sqlite" or not use_distinct:
            if connection.vendor == "postgresql":
                # Create a subquery of all contentnodes deduped by content_id
                # to avoid calling distinct on an annotated queryset.
                deduped_ids = self.model.objects.order_by("content_id").distinct(
                    "content_id"
                )
            else:
                # adapted from https://code.djangoproject.com/ticket/22696
                deduped_ids = (
                    self.values("content_id")
                    .annotate(node_id=Min("id"))
                    .values_list("node_id", flat=True)
                )
            return self.filter_by_uuids(deduped_ids)

        # when using postgres, we can call distinct on a specific column
        elif connection.vendor == "postgresql":
            return self.order_by("content_id").distinct("content_id")

    def filter_by_content_ids(self, content_ids, validate=True):
        return self._by_uuids(content_ids, validate, "content_id", True)

    def exclude_by_content_ids(self, content_ids, validate=True):
        return self._by_uuids(content_ids, validate, "content_id", False)

    def has_all_labels(self, field_name, labels):
        bitmasks = metadata_bitmasks[field_name]
        bits = {}
        for label in labels:
            if label in bitmasks:
                bitmask_fieldname = bitmasks[label]["bitmask_field_name"]
                if bitmask_fieldname not in bits:
                    bits[bitmask_fieldname] = 0
                bits[bitmask_fieldname] += bitmasks[label]["bits"]

        filters = {}
        annotations = {}
        for bitmask_fieldname, bits in bits.items():
            annotation_fieldname = "{}_{}".format(bitmask_fieldname, "masked")
            filters[annotation_fieldname + "__gt"] = 0
            annotations[annotation_fieldname] = F(bitmask_fieldname).bitand(bits)

        return self.annotate(**annotations).filter(**filters)


class ContentNodeManager(
    models.Manager.from_queryset(ContentNodeQueryset), TreeManager
):
    def get_queryset(self, *args, **kwargs):
        """
        Ensures that this manager always returns nodes in tree order.
        """
        return (
            super(TreeManager, self)
            .get_queryset(*args, **kwargs)
            .order_by(self.tree_id_attr, self.left_attr)
        )

    def build_tree_nodes(self, data, target=None, position="last-child"):
        """
        vendored from:
        https://github.com/django-mptt/django-mptt/blob/fe2b9cc8cfd8f4b764d294747dba2758147712eb/mptt/managers.py#L614
        """
        opts = self.model._mptt_meta
        if target:
            tree_id = target.tree_id
            if position in ("left", "right"):
                level = getattr(target, opts.level_attr)
                if position == "left":
                    cursor = getattr(target, opts.left_attr)
                else:
                    cursor = getattr(target, opts.right_attr) + 1
            else:
                level = getattr(target, opts.level_attr) + 1
                if position == "first-child":
                    cursor = getattr(target, opts.left_attr) + 1
                else:
                    cursor = getattr(target, opts.right_attr)
        else:
            tree_id = self._get_next_tree_id()
            cursor = 1
            level = 0

        stack = []

        def treeify(data, cursor=1, level=0):
            data = dict(data)
            children = data.pop("children", [])
            node = self.model(**data)
            stack.append(node)
            setattr(node, opts.tree_id_attr, tree_id)
            setattr(node, opts.level_attr, level)
            setattr(node, opts.left_attr, cursor)
            for child in children:
                cursor = treeify(child, cursor=cursor + 1, level=level + 1)
            cursor += 1
            setattr(node, opts.right_attr, cursor)
            return cursor

        treeify(data, cursor=cursor, level=level)

        if target:
            self._create_space(2 * len(stack), cursor - 1, tree_id)

        return stack


@python_2_unicode_compatible
class ContentNode(base_models.ContentNode):
    """
    The primary object type in a content database. Defines the properties that are shared
    across all content types.

    It represents videos, exercises, audio, documents, and other 'content items' that
    exist as nodes in content channels.
    """

    # Fields used only on Kolibri and not imported from a content database
    # Total number of coach only resources for this node
    num_coach_contents = models.IntegerField(default=0, null=True, blank=True)
    # Total number of available resources on the device under this topic - if this is not a topic
    # then it is 1 or 0 depending on availability
    on_device_resources = models.IntegerField(default=0, null=True, blank=True)

    # Use this to annotate ancestor information directly onto the ContentNode, as it can be a
    # costly lookup
    # Don't use strict loading as the titles used to construct the ancestors can contain
    # control characters, which will fail strict loading.
    ancestors = JSONField(
        default=[], null=True, blank=True, load_kwargs={"strict": False}
    )

    # This resource has been added to Kolibri directly or
    # indirectly by someone responsible for administering the device
    # whether a device super admin, or via initial configuration.
    # These nodes will not be subject to automatic garbage collection
    # to manage space.
    # Set as a NullBooleanField to limit migration time in creating the new column,
    # needs a subsequent Kolibri upgrade step to backfill these values.
    admin_imported = models.NullBooleanField()

    objects = ContentNodeManager()

    class Meta:
        ordering = ("lft",)
        index_together = [
            ["level", "channel_id", "kind"],
            ["level", "channel_id", "available"],
        ]

    def __str__(self):
        return self.title

    def get_descendant_content_ids(self):
        """
        Retrieve a queryset of content_ids for non-topic content nodes that are
        descendants of this node.
        """
        return (
            ContentNode.objects.filter(lft__gte=self.lft, lft__lte=self.rght)
            .exclude(kind=content_kinds.TOPIC)
            .values_list("content_id", flat=True)
        )


for field_name in bitmask_fieldnames:
    field = models.BigIntegerField(default=0, null=True, blank=True)
    field.contribute_to_class(ContentNode, field_name)


@python_2_unicode_compatible
class Language(base_models.Language):
    def __str__(self):
        return self.lang_name or ""


class File(base_models.File):
    """
    The second to bottom layer of the contentDB schema, defines the basic building brick for content.
    Things it can represent are, for example, mp4, avi, mov, html, css, jpeg, pdf, mp3...
    """

    class Meta:
        ordering = ["priority"]

    class Admin:
        pass

    def get_extension(self):
        return self.local_file.extension

    def get_file_size(self):
        return self.local_file.file_size

    def get_storage_url(self):
        return self.local_file.get_storage_url()

    def get_preset(self):
        """
        Return the preset.
        """
        return PRESET_LOOKUP.get(self.preset, _("Unknown format"))


class LocalFileQueryset(models.QuerySet, FilterByUUIDQuerysetMixin):
    def delete_unused_files(self):
        for file in self.get_unused_files():
            try:
                os.remove(paths.get_content_storage_file_path(file.get_filename()))
                yield True, file
            except (IOError, OSError, InvalidStorageFilenameError):
                yield False, file
        self.get_unused_files().update(available=False)

    def get_orphan_files(self):
        return self.filter(files__isnull=True)

    def delete_orphan_file_objects(self):
        return self.filter(files__isnull=True).delete()

    def get_unused_files(self):
        ids = LocalFile.objects.filter(
            Q(files__contentnode__available=False) | Q(files__isnull=True)
        )
        return (
            self.filter(id__in=ids)
            .exclude(files__contentnode__available=True)
            .filter(available=True)
        )


@python_2_unicode_compatible
class LocalFile(base_models.LocalFile):
    """
    The bottom layer of the contentDB schema, defines the local state of files on the device storage.
    """

    objects = LocalFileQueryset.as_manager()

    class Admin:
        pass

    def __str__(self):
        return paths.get_content_file_name(self)

    def get_filename(self):
        return self.__str__()

    def get_storage_url(self):
        """
        Return a url for the client side to retrieve the content file.
        The same url will also be exposed by the file serializer.
        """
        return paths.get_local_content_storage_file_url(self)

    def delete_stored_file(self):
        """
        Delete the stored file from disk.
        """
        deleted = False

        try:
            os.remove(paths.get_content_storage_file_path(self.get_filename()))
            deleted = True
        except (IOError, OSError, InvalidStorageFilenameError):
            deleted = False

        self.available = False
        self.save()
        return deleted


class AssessmentMetaData(base_models.AssessmentMetaData):
    """
    A model to describe additional metadata that characterizes assessment behaviour in Kolibri.
    This model contains additional fields that are only revelant to content nodes that probe a
    user's state of knowledge and allow them to practice to Mastery.
    ContentNodes with this metadata may also be able to be used within quizzes and exams.
    """

    pass


class ChannelMetadataQueryset(QuerySet, FilterByUUIDQuerysetMixin):
    pass


@python_2_unicode_compatible
class ChannelMetadata(base_models.ChannelMetadata):
    """
    Holds metadata about all existing content databases that exist locally.
    """

    # precalculated fields during annotation/migration
    published_size = models.BigIntegerField(default=0, null=True, blank=True)
    total_resource_count = models.IntegerField(default=0, null=True, blank=True)
    included_languages = models.ManyToManyField(
        "Language", related_name="channels", verbose_name="languages", blank=True
    )
    order = models.PositiveIntegerField(default=0, null=True, blank=True)
    public = models.NullBooleanField()
    # Has only a subset of this channel's metadata been imported?
    # Use a null boolean field to avoid issues during metadata import
    partial = models.NullBooleanField(default=False)

    objects = ChannelMetadataQueryset.as_manager()

    class Admin:
        pass

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.name

    def delete_content_tree_and_files(self):
        # Use Django ORM to ensure cascading delete:
        self.root.delete()
        ContentCacheKey.update_cache_key()


class ContentRequestType(ChoicesEnum):
    Download = "DOWNLOAD"
    Removal = "REMOVAL"


class ContentRequestReason(ChoicesEnum):
    UserInitiated = "USER_INITIATED"
    SyncInitiated = "SYNC_INITIATED"


class ContentRequestStatus(ChoicesEnum):
    Pending = "PENDING"
    InProgress = "IN_PROGRESS"
    Failed = "FAILED"
    Completed = "COMPLETED"


def _hex_uuid_str():
    return str(uuid.uuid4().hex)


class ContentRequest(models.Model):
    """
    Model representing requests for specific content, either through user interaction or as a
    consequence of a sync. This stores their intermediate state as well as whether they've been
    downloaded or removed
    """

    id = UUIDField(primary_key=True, default=_hex_uuid_str)
    facility = models.ForeignKey(Facility, related_name="content_requests")

    # the source model's `morango_model_name` that initiated the request:
    # - for user-initiated requests it should be `facilityuser`
    # - for sync-initiated requests it should the model that assigned the content (lesson, exam)
    # and max_length=40 is the same value used in morango's Store.model_name
    source_model = models.CharField(max_length=40)
    # the source model's PK, could be the user's ID
    source_id = UUIDField()

    requested_at = DateTimeTzField(default=local_now)

    type = models.CharField(choices=ContentRequestType.choices(), max_length=8)
    reason = models.CharField(choices=ContentRequestReason.choices(), max_length=14)
    status = models.CharField(choices=ContentRequestStatus.choices(), max_length=11)

    contentnode_id = UUIDField()
    metadata = JSONField(null=True)

    class Meta:
        unique_together = ("type", "source_model", "source_id", "contentnode_id")
        ordering = ("requested_at",)

    def save(self, *args, **kwargs):
        """
        Save override to set type for the proxy models
        """
        self.type = getattr(self.__class__.objects, "request_type", None)
        return super(ContentRequest, self).save(*args, **kwargs)

    @classmethod
    def build_for_user(cls, user):
        """
        :type user: FacilityUser
        :return: A ContentRequest
        :rtype: ContentRequest
        """
        return cls(
            facility_id=user.facility_id,
            source_model=FacilityUser.morango_model_name,
            source_id=user.id,
            type=getattr(cls.objects, "request_type", None),
            reason=ContentRequestReason.UserInitiated,
            status=ContentRequestStatus.Pending,
        )


class ContentRequestManager(models.Manager):
    request_type = None

    def get_queryset(self):
        """
        Automatically filters on the request type for use with proxy models
        :rtype: django.db.models.QuerySet
        """
        queryset = super(ContentRequestManager, self).get_queryset()
        return queryset.filter(type=self.request_type)


class ContentDownloadRequestManager(ContentRequestManager):
    request_type = ContentRequestType.Download


class ContentDownloadRequest(ContentRequest):
    """
    Proxy model for the Download content request type
    """

    objects = ContentDownloadRequestManager()

    class Meta:
        proxy = True


class ContentRemovalRequestManager(ContentRequestManager):
    request_type = ContentRequestType.Removal


class ContentRemovalRequest(ContentRequest):
    """
    Proxy model for the Removal content request type
    """

    objects = ContentRemovalRequestManager()

    class Meta:
        proxy = True
