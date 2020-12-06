from functools import reduce
from itertools import groupby
from sqlalchemy import BigInteger, desc
from sqlalchemy.orm import reconstructor
from sqlalchemy.orm.collections import InstrumentedList

from sqlalchemy_api_handler.bases.accessor import Accessor
from sqlalchemy_api_handler.bases.errors import ActivityError
from sqlalchemy_api_handler.bases.save import Save
from sqlalchemy_api_handler.utils.datum import relationships_in
from sqlalchemy_api_handler.utils.logger import logger


def merged_datum_from_activities(activities,
                                 model,
                                 initial=None):
    return reduce(lambda agg, activity: {**agg, **relationships_in(activity.patch, model)},
                  activities,
                  relationships_in(initial, model) if initial else {})


def set_joined_relationship_activity_identifiers(self):
    for key in self.__mapper__.relationships.keys():
        if key == 'transaction' \
           or isinstance(getattr(self, key), InstrumentedList):
            return
        relationship_activity_identifier_key =  f'{key}ActivityIdentifier'
        if not hasattr(self, relationship_activity_identifier_key):
            def get_relationship_activity_identifier(entity):
                relationship = getattr(entity, key)
                return relationship.activityIdentifier if hasattr(relationship, 'activityIdentifier') \
                                                          else None
            setattr(self.__class__,
                    relationship_activity_identifier_key,
                    property(get_relationship_activity_identifier))


class Activator(Save):

    has_set_relationship_activity_identifier_property = False

    def __init__(self, **initial_datum):
        set_joined_relationship_activity_identifiers(self)
        Save.__init__(self, **initial_datum)

    @reconstructor
    def init_on_load(self):
        set_joined_relationship_activity_identifiers(self)

    @classmethod
    def get_activity(cls):
        return Activator.activity_cls

    @classmethod
    def set_activity(cls, activity_cls):
        Activator.activity_cls = activity_cls

    @staticmethod
    def activate(*activities):
        Activity = Activator.get_activity()
        for (entity_identifier, grouped_activities) in groupby(activities, key=lambda activity: activity.entityIdentifier):
            grouped_activities = sorted(grouped_activities, key=lambda activity: activity.dateCreated)

            first_activity = grouped_activities[0]
            table_name = first_activity.tableName
            model = Save.model_from_table_name(table_name)
            if model is None:
                errors = ActivityError()
                errors.add_error('tableName', 'model from {} not found'.format(table_name))
                raise errors

            id_key = model.id.property.key
            entity_id = first_activity.old_data.get(id_key) \
                        if first_activity.old_data else None

            if not entity_id:
                entity = model.query.filter_by(activityIdentifier=entity_identifier).first()
                entity_id = entity.id if entity else None

            if not entity_id:
                entity = model(**relationships_in(first_activity.patch, model))
                entity.activityIdentifier = entity_identifier
                Activator.save(entity)
                query_filter = (Activity.table_name == model.__tablename__) & \
                               (Activity.entityIdentifier == entity_identifier) & \
                               (Activity.verb == 'insert')
                insert_activity = Activity.query.filter(query_filter).one()
                insert_activity.dateCreated = first_activity.dateCreated
                Save.save(insert_activity)
                # want to make as if first_activity was the inser_activity one
                # for such route like operations
                # '''
                #    ApiHandler.activate(**activities)
                #    return jsonify([as_dict(activity) for activity in activities])
                # '''
                first_activity.id = insert_activity.id
                first_activity.changed_data = {**insert_activity.changed_data}
                if insert_activity.transaction:
                    first_activity.transaction = Activity.transaction.mapper.class_()
                    first_activity.transaction.actor  = insert_activity.transaction.actor

                for activity in grouped_activities[1:]:
                    activity.old_data = { id_key: entity.id }
                Activator.activate(*grouped_activities[1:])
                continue

            min_date = min(map(lambda a: a.dateCreated, grouped_activities))
            already_activities_since_min_date = Activity.query \
                                                        .filter(
                                                            (Activity.tableName == table_name) & \
                                                            (Activity.data[id_key].astext.cast(BigInteger) == entity_id) & \
                                                            (Activity.dateCreated >= min_date)
                                                        ) \
                                                        .all()
            Save.save(*grouped_activities)
            all_activities_since_min_date = sorted(already_activities_since_min_date + grouped_activities,
                                                   key=lambda activity: activity.dateCreated)
            datum = merged_datum_from_activities(all_activities_since_min_date,
                                                 model,
                                                 initial=all_activities_since_min_date[0].datum)
            if model.id.key in datum:
                del datum[model.id.key]
            datum['activityIdentifier'] = entity_identifier
            entity = model.query.get(entity_id)
            entity.modify(datum)
            Activator.save(entity)

    @classmethod
    def models(cls):
        models = Accessor.models()
        Activity = cls.get_activity()
        if Activity:
            models += [Activity]
        return models

    @classmethod
    def save(cls, *entities):
        Activity = Activator.get_activity()
        Save.save(*entities)
        activities = []
        for entity in entities:
            if hasattr(entity, 'activityIdentifier'):
                id_key = entity.__class__.id.property.key
                last_activity = Activity.query.filter(
                    (Activity.tableName == entity.__tablename__) & \
                    (Activity.data[id_key].astext.cast(BigInteger) == entity.id)
                ).order_by(desc(Activity.dateCreated)).limit(1).first()
                if not last_activity:
                    logger.debug('last_activity not found for {} {}...'.format(entity.__class__.__name__, entity.id))
                    continue
                last_activity.entityIdentifier = entity.activityIdentifier
                activities.append(last_activity)
        Save.save(*activities)
