import os

from peewee import BooleanField, CharField, DateTimeField, Model, IntegerField
from playhouse.db_url import parse
from playhouse.postgres_ext import (
    BigIntegerField,
    DecimalField,
    DateField,
    BinaryJSONField,
    TSVectorField,
    TextField,
    PostgresqlDatabase,
)

strolid_cxm_db_url = os.environ["STROLID_CXM_DATABASE_URL"]
db_params = parse(strolid_cxm_db_url)
db_name = db_params["database"]
del db_params["database"]
database = PostgresqlDatabase(db_name, autocommit=True, autorollback=True, **db_params)


class BaseModel(Model):
    class Meta:
        database = database
        legacy_table_names = False


class ShelbyLead(BaseModel):
    id = IntegerField(primary_key=True, index=True)

    assigned_to_id = IntegerField(index=True, null=True)
    back_end_profit = DecimalField()
    bad_by_id = IntegerField(null=True)
    bad_on = DateTimeField(null=True)
    canonical_lead_id = IntegerField(index=True, null=True)
    clock_stopped_on = DateTimeField(null=True)
    closed_by_id = IntegerField(null=True)
    closed_on = DateTimeField(null=True)
    created_by_id = IntegerField(null=True)
    created_on = DateTimeField(index=True)
    dealer_id = IntegerField(index=True)
    deduplicated_on = DateTimeField(null=True)
    details = BinaryJSONField()
    front_end_profit = DecimalField()
    fts_field = TextField(null=True)
    fts_field_search = TSVectorField(index=True, null=True)
    handled_override = BooleanField()
    house_deal = BooleanField()
    inactive_by_id = IntegerField(null=True)
    inactive_on = DateTimeField(null=True)
    is_strolid_chat = BooleanField()
    lead_type = CharField(index=True)
    lookup_details = BinaryJSONField()
    lookup_last_update_timestamp = DecimalField(index=True)
    merged_by_id = IntegerField(null=True)
    merged_on = DateTimeField(null=True)
    modified_on = DateTimeField()
    na_by_id = IntegerField(null=True)
    na_on = DateTimeField(null=True)
    possibly_sold_by_id = IntegerField(null=True)
    possibly_sold_on = DateTimeField(null=True)
    ready_to_handle_on = DateTimeField(index=True, null=True)
    reviewed_by_id = IntegerField(index=True, null=True)
    reviewed_on = DateTimeField(index=True, null=True)
    seen_by_group_flags = BigIntegerField()
    service_flags = BigIntegerField()
    sold_on = DateField(null=True)
    source_id = IntegerField(index=True, null=True)
    status = IntegerField()
    stock_no = CharField(null=True)
    translation_record_id = IntegerField(null=True)
    unclosed_by_id = IntegerField(null=True)
    unclosed_on = DateTimeField(null=True)

    class Meta:
        table_name = "shelby_lead"


# class ShelbyUser(BaseModel):
#     id = CharField(primary_key=True, index=True)
#     email = CharField(null=True)
#     name = CharField(null=True)
#     is_active = BooleanField(null=True)
#     zoho_contact_id = CharField(null=True)
#     extension = CharField(null=True)
#     enable_bria_recording = BooleanField(null=True)

#     class Meta:
#         table_name = "shelby_user"
