# -*- coding: utf-8 -*-
###########################################################################
# Copyright (c), The AiiDA team. All rights reserved.                     #
# This file is part of the AiiDA code.                                    #
#                                                                         #
# The code is hosted on GitHub at https://github.com/aiidateam/aiida-core #
# For further information on the license, see the LICENSE.txt file        #
# For further information please visit http://www.aiida.net               #
###########################################################################
# pylint: disable=too-many-nested-blocks,protected-access,fixme,too-many-arguments,too-many-locals,too-many-branches,too-many-statements
""" SQLAlchemy-specific import of AiiDA entities """

from distutils.version import StrictVersion
import os
import tarfile
import zipfile
from itertools import chain

from tqdm import tqdm

from aiida.common import timezone, json
from aiida.common.folders import SandboxFolder, RepositoryFolder
from aiida.common.links import LinkType
from aiida.common.utils import get_object_from_string
from aiida.orm import QueryBuilder, Node, Group, WorkflowNode, CalculationNode, Data
from aiida.orm.utils.links import link_triple_exists, validate_link
from aiida.orm.utils.repository import Repository

from aiida.tools.importexport.common import exceptions
from aiida.tools.importexport.common.archive import extract_tree, extract_tar, extract_zip
from aiida.tools.importexport.common.config import (
    DUPL_SUFFIX, IMPORTGROUP_TYPE, EXPORT_VERSION, NODES_EXPORT_SUBFOLDER, BAR_FORMAT
)
from aiida.tools.importexport.common.config import (
    NODE_ENTITY_NAME, GROUP_ENTITY_NAME, COMPUTER_ENTITY_NAME, USER_ENTITY_NAME, LOG_ENTITY_NAME, COMMENT_ENTITY_NAME
)
from aiida.tools.importexport.common.config import (
    entity_names_to_signatures, signatures_to_entity_names, entity_names_to_sqla_schema, file_fields_to_model_fields,
    entity_names_to_entities
)
from aiida.tools.importexport.common.utils import export_shard_uuid
from aiida.tools.importexport.dbimport.utils import (
    deserialize_field, merge_comment, merge_extras, start_header, finish_header
)
from aiida.tools.importexport.dbimport.backends.sqla.utils import validate_uuid


def import_data_sqla(
    in_path,
    group=None,
    ignore_unknown_nodes=False,
    extras_mode_existing='kcl',
    extras_mode_new='import',
    comment_mode='newest',
    silent=False,
    debug=False,
    **kwargs
):
    """Import exported AiiDA archive to the AiiDA database and repository.

    Specific for the SQLAlchemy backend.
    If ``in_path`` is a folder, calls extract_tree; otherwise, tries to detect the compression format
    (zip, tar.gz, tar.bz2, ...) and calls the correct function.

    :param in_path: the path to a file or folder that can be imported in AiiDA.
    :type in_path: str

    :param group: Group wherein all imported Nodes will be placed.
    :type group: :py:class:`~aiida.orm.groups.Group`

    :param extras_mode_existing: 3 letter code that will identify what to do with the extras import.
        The first letter acts on extras that are present in the original node and not present in the imported node.
        Can be either:
        'k' (keep it) or
        'n' (do not keep it).
        The second letter acts on the imported extras that are not present in the original node.
        Can be either:
        'c' (create it) or
        'n' (do not create it).
        The third letter defines what to do in case of a name collision.
        Can be either:
        'l' (leave the old value),
        'u' (update with a new value),
        'd' (delete the extra), or
        'a' (ask what to do if the content is different).
    :type extras_mode_existing: str

    :param extras_mode_new: 'import' to import extras of new nodes or 'none' to ignore them.
    :type extras_mode_new: str

    :param comment_mode: Comment import modes (when same UUIDs are found).
        Can be either:
        'newest' (will keep the Comment with the most recent modification time (mtime)) or
        'overwrite' (will overwrite existing Comments with the ones from the import file).
    :type comment_mode: str

    :param silent: suppress prints.
    :type silent: bool

    :param debug: Whether or not to print helpful debug messages (will mess up the progress bar a bit).
    :type debug: bool

    :return: New and existing Nodes and Links.
    :rtype: dict

    :raises `~aiida.tools.importexport.common.exceptions.ImportValidationError`: if parameters or the contents of
        `metadata.json` or `data.json` can not be validated.
    :raises `~aiida.tools.importexport.common.exceptions.CorruptArchive`: if the provided archive at ``in_path`` is
        corrupted.
    :raises `~aiida.tools.importexport.common.exceptions.IncompatibleArchiveVersionError`: if the provided archive's
        export version is not equal to the export version of AiiDA at the moment of import.
    :raises `~aiida.tools.importexport.common.exceptions.ArchiveImportError`: if there are any internal errors when
        importing.
    :raises `~aiida.tools.importexport.common.exceptions.ImportUniquenessError`: if a new unique entity can not be
        created.
    """
    from aiida.backends.sqlalchemy.models.node import DbNode, DbLink
    from aiida.backends.sqlalchemy.utils import flag_modified

    # This is the export version expected by this function
    expected_export_version = StrictVersion(EXPORT_VERSION)

    # The returned dictionary with new and existing nodes and links
    ret_dict = {}

    # Initial check(s)
    if group:
        if not isinstance(group, Group):
            raise exceptions.ImportValidationError('group must be a Group entity')
        elif not group.is_stored:
            group.store()

    ################
    # EXTRACT DATA #
    ################
    # The sandbox has to remain open until the end
    with SandboxFolder() as folder:
        if os.path.isdir(in_path):
            extract_tree(in_path, folder)
        else:
            if tarfile.is_tarfile(in_path):
                extract_tar(in_path, folder, silent=silent, nodes_export_subfolder=NODES_EXPORT_SUBFOLDER, **kwargs)
            elif zipfile.is_zipfile(in_path):
                extract_zip(in_path, folder, silent=silent, nodes_export_subfolder=NODES_EXPORT_SUBFOLDER, **kwargs)
            else:
                raise exceptions.ImportValidationError(
                    'Unable to detect the input file format, it is neither a '
                    'tar file, nor a (possibly compressed) zip file.'
                )

        if not folder.get_content_list():
            raise exceptions.CorruptArchive('The provided file/folder ({}) is empty'.format(in_path))
        try:
            if debug:
                print('CACHING metadata.json')
            with open(folder.get_abs_path('metadata.json'), encoding='utf8') as fhandle:
                metadata = json.load(fhandle)

            if debug:
                print('CACHING data.json')
            with open(folder.get_abs_path('data.json'), encoding='utf8') as fhandle:
                data = json.load(fhandle)
        except IOError as error:
            raise exceptions.CorruptArchive(
                'Unable to find the file {} in the import file or folder'.format(error.filename)
            )

        ######################
        # PRELIMINARY CHECKS #
        ######################
        export_version = StrictVersion(str(metadata['export_version']))
        if export_version != expected_export_version:
            msg = 'Export file version is {}, can import only version {}'\
                    .format(metadata['export_version'], expected_export_version)
            if export_version < expected_export_version:
                msg += "\nUse 'verdi export migrate' to update this export file."
            else:
                msg += '\nUpdate your AiiDA version in order to import this file.'

            raise exceptions.IncompatibleArchiveVersionError(msg)

        if not silent:
            start_header(in_path, comment_mode, extras_mode_new, extras_mode_existing, debug)

        ###################################################################
        #           CREATE UUID REVERSE TABLES AND CHECK IF               #
        #              I HAVE ALL NODES FOR THE LINKS                     #
        ###################################################################
        if debug:
            print('CHECKING IF NODES FROM LINKS ARE IN DB OR ARCHIVE...')

        linked_nodes = set(chain.from_iterable((l['input'], l['output']) for l in data['links_uuid']))
        group_nodes = set(chain.from_iterable(data['groups_uuid'].values()))

        # Check that UUIDs are valid
        linked_nodes = set(x for x in linked_nodes if validate_uuid(x))
        group_nodes = set(x for x in group_nodes if validate_uuid(x))

        import_nodes_uuid = set()
        for value in data['export_data'].get(NODE_ENTITY_NAME, {}).values():
            import_nodes_uuid.add(value['uuid'])

        unknown_nodes = linked_nodes.union(group_nodes) - import_nodes_uuid

        if unknown_nodes and not ignore_unknown_nodes:
            raise exceptions.DanglingLinkError(
                'The import file refers to {} nodes with unknown UUID, therefore it cannot be imported. Either first '
                'import the unknown nodes, or export also the parents when exporting. The unknown UUIDs are:\n'
                ''.format(len(unknown_nodes)) + '\n'.join('* {}'.format(uuid) for uuid in unknown_nodes)
            )

        ###################################
        # DOUBLE-CHECK MODEL DEPENDENCIES #
        ###################################
        # The entity import order. It is defined by the database model relationships.
        entity_order = [
            USER_ENTITY_NAME, COMPUTER_ENTITY_NAME, NODE_ENTITY_NAME, GROUP_ENTITY_NAME, LOG_ENTITY_NAME,
            COMMENT_ENTITY_NAME
        ]

        #  I make a new list that contains the entity names:
        # eg: ['User', 'Computer', 'Node', 'Group']
        for import_field_name in metadata['all_fields_info']:
            if import_field_name not in entity_order:
                raise exceptions.ImportValidationError(
                    "You are trying to import an unknown model '{}'!".format(import_field_name)
                )

        for idx, entity_name in enumerate(entity_order):
            dependencies = []
            # for every field, I checked the dependencies given as value for key requires
            for field in metadata['all_fields_info'][entity_name].values():
                try:
                    dependencies.append(field['requires'])
                except KeyError:
                    # (No ForeignKey)
                    pass
            for dependency in dependencies:
                if dependency not in entity_order[:idx]:
                    raise exceptions.ArchiveImportError(
                        'Entity {} requires {} but would be loaded first; stopping...'.format(entity_name, dependency)
                    )

        ###################################################
        # CREATE IMPORT DATA DIRECT UNIQUE_FIELD MAPPINGS #
        ###################################################
        # This is nested dictionary of entity_name:{id:uuid}
        # to map one id (the pk) to a different one.
        # One of the things to remove for v0.4
        # {
        # 'Node': {2362: '82a897b5-fb3a-47d7-8b22-c5fe1b4f2c14',
        #           2363: 'ef04aa5d-99e7-4bfd-95ef-fe412a6a3524', 2364: '1dc59576-af21-4d71-81c2-bac1fc82a84a'},
        # 'User': {1: 'aiida@localhost'}
        # }
        if debug:
            print('CREATING PK-2-UUID/EMAIL MAPPING...')
        import_unique_ids_mappings = {}
        # Export data since v0.3 contains the keys entity_name
        for entity_name, import_data in data['export_data'].items():
            # Again I need the entity_name since that's what's being stored since 0.3
            if entity_name in metadata['unique_identifiers']:
                # I have to reconvert the pk to integer
                import_unique_ids_mappings[entity_name] = {
                    int(k): v[metadata['unique_identifiers'][entity_name]] for k, v in import_data.items()
                }
        ###############
        # IMPORT DATA #
        ###############
        # DO ALL WITH A TRANSACTION
        import aiida.backends.sqlalchemy

        session = aiida.backends.sqlalchemy.get_scoped_session()

        try:
            foreign_ids_reverse_mappings = {}
            new_entries = {}
            existing_entries = {}

            if debug:
                print('GENERATING LIST OF DATA...')

            if not silent:
                # Instantiate progress bar
                if 'progress_bar' in kwargs:
                    progress_bar = kwargs['progress_bar']
                    progress_bar.bar_format = BAR_FORMAT
                    progress_bar.total = 1
                    progress_bar.leave = True
                    progress_bar.disable = False
                else:
                    progress_bar = tqdm(total=1, bar_format=BAR_FORMAT, leave=True)
                pbar_base_str = 'Generating list of data - '

                # Get total entities from data.json
                # To be used with progress bar
                number_of_entities = 0

            # I first generate the list of data
            for entity_name in entity_order:
                entity = entity_names_to_entities[entity_name]
                # I get the unique identifier, since v0.3 stored under entity_name
                unique_identifier = metadata['unique_identifiers'].get(entity_name, None)

                # so, new_entries. Also, since v0.3 it makes more sense to use the entity_name
                new_entries[entity_name] = {}
                existing_entries[entity_name] = {}
                foreign_ids_reverse_mappings[entity_name] = {}

                # Not necessarily all models are exported
                if entity_name in data['export_data']:

                    if debug:
                        print('  {}...'.format(entity_name))

                    if not silent:
                        progress_bar.set_description_str(pbar_base_str + entity_name)
                        number_of_entities += len(data['export_data'][entity_name])

                    if unique_identifier is not None:
                        import_unique_ids = set(v[unique_identifier] for v in data['export_data'][entity_name].values())

                        relevant_db_entries = {}
                        if import_unique_ids:
                            builder = QueryBuilder()
                            builder.append(entity, filters={unique_identifier: {'in': import_unique_ids}}, project='*')

                            if silent:
                                relevant_db_entries = {getattr(v[0], unique_identifier): v[0] for v in builder.all()}
                            elif builder.count():
                                progress_bar.reset(total=builder.count())
                                for object_ in builder.iterall():
                                    progress_bar.update()

                                    relevant_db_entries.update({getattr(object_[0], unique_identifier): object_[0]})

                            foreign_ids_reverse_mappings[entity_name] = {
                                k: v.pk for k, v in relevant_db_entries.items()
                            }

                        if debug:
                            print('    GOING THROUGH ARCHIVE...')

                        imported_comp_names = set()
                        for key, value in data['export_data'][entity_name].items():
                            if entity_name == GROUP_ENTITY_NAME:
                                # Check if there is already a group with the same name,
                                # and if so, recreate the name
                                orig_label = value['label']
                                dupl_counter = 0
                                while QueryBuilder().append(entity, filters={'label': {'==': value['label']}}).count():
                                    # Rename the new group
                                    value['label'] = orig_label + DUPL_SUFFIX.format(dupl_counter)
                                    dupl_counter += 1
                                    if dupl_counter == 100:
                                        raise exceptions.ImportUniquenessError(
                                            'A group of that label ( {} ) already exists and I could not create a new '
                                            'one'.format(orig_label)
                                        )

                            elif entity_name == COMPUTER_ENTITY_NAME:
                                # The following is done for compatibility
                                # reasons in case the export file was generated
                                # with the Django export method. In Django the
                                # metadata and the transport parameters are
                                # stored as (unicode) strings of the serialized
                                # JSON objects and not as simple serialized
                                # JSON objects.
                                if isinstance(value['metadata'], (str, bytes)):
                                    value['metadata'] = json.loads(value['metadata'])

                                # Check if there is already a computer with the
                                # same name in the database
                                builder = QueryBuilder()
                                builder.append(
                                    entity, filters={'name': {
                                        '==': value['name']
                                    }}, project=['*'], tag='res'
                                )
                                dupl = (builder.count() or value['name'] in imported_comp_names)
                                dupl_counter = 0
                                orig_name = value['name']
                                while dupl:
                                    # Rename the new computer
                                    value['name'] = (orig_name + DUPL_SUFFIX.format(dupl_counter))
                                    builder = QueryBuilder()
                                    builder.append(
                                        entity, filters={'name': {
                                            '==': value['name']
                                        }}, project=['*'], tag='res'
                                    )
                                    dupl = (builder.count() or value['name'] in imported_comp_names)
                                    dupl_counter += 1
                                    if dupl_counter == 100:
                                        raise exceptions.ImportUniquenessError(
                                            'A computer of that name ( {} ) already exists and I could not create a '
                                            'new one'.format(orig_name)
                                        )

                                imported_comp_names.add(value['name'])

                            if value[unique_identifier] in relevant_db_entries:
                                # Already in DB
                                # again, switched to entity_name in v0.3
                                existing_entries[entity_name][key] = value
                            else:
                                # To be added
                                new_entries[entity_name][key] = value
                    else:
                        # Why the copy:
                        new_entries[entity_name] = data['export_data'][entity_name].copy()

            if not silent:
                # Progress bar - reset for import
                progress_bar.reset(total=number_of_entities)
                reset_progress_bar = {}

            # I import data from the given model
            for entity_name in entity_order:
                entity = entity_names_to_entities[entity_name]
                fields_info = metadata['all_fields_info'].get(entity_name, {})
                unique_identifier = metadata['unique_identifiers'].get(entity_name, '')

                if not silent:
                    # Progress bar initialization - Model
                    if reset_progress_bar:
                        progress_bar.reset(total=reset_progress_bar['total'])
                        progress_bar.n = reset_progress_bar['n']
                        reset_progress_bar = {}
                    pbar_base_str = '{}s - '.format(entity_name)
                    progress_bar.set_description_str(pbar_base_str + 'Initializing')

                # EXISTING ENTRIES
                if not silent and existing_entries[entity_name]:
                    # Progress bar update - Model
                    progress_bar.set_description_str(
                        pbar_base_str + '{} existing entries'.format(len(existing_entries[entity_name]))
                    )

                for import_entry_pk, entry_data in existing_entries[entity_name].items():
                    unique_id = entry_data[unique_identifier]
                    existing_entry_pk = foreign_ids_reverse_mappings[entity_name][unique_id]
                    import_data = dict(
                        deserialize_field(
                            k,
                            v,
                            fields_info=fields_info,
                            import_unique_ids_mappings=import_unique_ids_mappings,
                            foreign_ids_reverse_mappings=foreign_ids_reverse_mappings
                        ) for k, v in entry_data.items()
                    )
                    # TODO COMPARE, AND COMPARE ATTRIBUTES

                    if entity_name == COMMENT_ENTITY_NAME:
                        new_entry_uuid = merge_comment(import_data, comment_mode)
                        if new_entry_uuid is not None:
                            entry_data[unique_identifier] = new_entry_uuid
                            new_entries[entity_name][import_entry_pk] = entry_data

                    if entity_name not in ret_dict:
                        ret_dict[entity_name] = {'new': [], 'existing': []}
                    ret_dict[entity_name]['existing'].append((import_entry_pk, existing_entry_pk))
                    if debug:
                        print('existing %s: %s (%s->%s)' % (entity_name, unique_id, import_entry_pk, existing_entry_pk))

                # Store all objects for this model in a list, and store them
                # all in once at the end.
                objects_to_create = list()
                # In the following list we add the objects to be updated
                objects_to_update = list()
                # This is needed later to associate the import entry with the new pk
                import_new_entry_pks = dict()

                # NEW ENTRIES
                if not silent and new_entries[entity_name]:
                    # Progress bar update - Model
                    progress_bar.set_description_str(
                        pbar_base_str + '{} new entries'.format(len(new_entries[entity_name]))
                    )

                for import_entry_pk, entry_data in new_entries[entity_name].items():
                    unique_id = entry_data[unique_identifier]
                    import_data = dict(
                        deserialize_field(
                            k,
                            v,
                            fields_info=fields_info,
                            import_unique_ids_mappings=import_unique_ids_mappings,
                            foreign_ids_reverse_mappings=foreign_ids_reverse_mappings
                        ) for k, v in entry_data.items()
                    )

                    # We convert the Django fields to SQLA. Note that some of
                    # the Django fields were converted to SQLA compatible
                    # fields by the deserialize_field method. This was done
                    # for optimization reasons in Django but makes them
                    # compatible with the SQLA schema and they don't need any
                    # further conversion.
                    if entity_name in file_fields_to_model_fields:
                        for file_fkey in file_fields_to_model_fields[entity_name]:

                            # This is an exception because the DbLog model defines the `_metadata` column instead of the
                            # `metadata` column used in the Django model. This is because the SqlAlchemy model base
                            # class already has a metadata attribute that cannot be overridden. For consistency, the
                            # `DbLog` class however expects the `metadata` keyword in its constructor, so we should
                            # ignore the mapping here
                            if entity_name == LOG_ENTITY_NAME and file_fkey == 'metadata':
                                continue

                            model_fkey = file_fields_to_model_fields[entity_name][file_fkey]
                            if model_fkey in import_data:
                                continue
                            import_data[model_fkey] = import_data[file_fkey]
                            import_data.pop(file_fkey, None)

                    db_entity = get_object_from_string(entity_names_to_sqla_schema[entity_name])

                    objects_to_create.append(db_entity(**import_data))
                    import_new_entry_pks[unique_id] = import_entry_pk

                if entity_name == NODE_ENTITY_NAME:
                    if debug:
                        print('STORING NEW NODE REPOSITORY FILES & ATTRIBUTES...')

                    # NEW NODES
                    for object_ in objects_to_create:
                        import_entry_uuid = object_.uuid
                        import_entry_pk = import_new_entry_pks[import_entry_uuid]

                        if not silent:
                            # Progress bar initialization - Node
                            progress_bar.update()
                            pbar_node_base_str = pbar_base_str + 'UUID={} - '.format(import_entry_uuid.split('-')[0])

                        # Before storing entries in the DB, I store the files (if these are nodes).
                        # Note: only for new entries!
                        subfolder = folder.get_subfolder(
                            os.path.join(NODES_EXPORT_SUBFOLDER, export_shard_uuid(import_entry_uuid))
                        )
                        if not subfolder.exists():
                            raise exceptions.CorruptArchive(
                                'Unable to find the repository folder for Node with UUID={} in the exported '
                                'file'.format(import_entry_uuid)
                            )
                        destdir = RepositoryFolder(section=Repository._section_name, uuid=import_entry_uuid)
                        # Replace the folder, possibly destroying existing previous folders, and move the files
                        # (faster if we are on the same filesystem, and in any case the source is a SandboxFolder)
                        if not silent:
                            progress_bar.set_description_str(pbar_node_base_str + 'Repository')
                        destdir.replace_with_folder(subfolder.abspath, move=True, overwrite=True)

                        # For Nodes, we also have to store Attributes!
                        if debug:
                            print('STORING NEW NODE ATTRIBUTES...')
                        if not silent:
                            progress_bar.set_description_str(pbar_node_base_str + 'Attributes')

                        # Get attributes from import file
                        try:
                            object_.attributes = data['node_attributes'][str(import_entry_pk)]
                        except KeyError:
                            raise exceptions.CorruptArchive(
                                'Unable to find attribute info for Node with UUID={}'.format(import_entry_uuid)
                            )

                        # For DbNodes, we also have to store extras
                        if extras_mode_new == 'import':
                            if debug:
                                print('STORING NEW NODE EXTRAS...')
                            if not silent:
                                progress_bar.set_description_str(pbar_node_base_str + 'Extras')

                            # Get extras from import file
                            try:
                                extras = data['node_extras'][str(import_entry_pk)]
                            except KeyError:
                                raise exceptions.CorruptArchive(
                                    'Unable to find extra info for Node with UUID={}'.format(import_entry_uuid)
                                )
                            # TODO: remove when aiida extras will be moved somewhere else
                            # from here
                            extras = {key: value for key, value in extras.items() if not key.startswith('_aiida_')}
                            if object_.node_type.endswith('code.Code.'):
                                extras = {key: value for key, value in extras.items() if not key == 'hidden'}
                            # till here
                            object_.extras = extras
                        elif extras_mode_new == 'none':
                            if debug:
                                print('SKIPPING NEW NODE EXTRAS...')
                        else:
                            raise exceptions.ImportValidationError(
                                "Unknown extras_mode_new value: {}, should be either 'import' or 'none'"
                                ''.format(extras_mode_new)
                            )

                    # EXISTING NODES (Extras)
                    if debug:
                        print('UPDATING EXISTING NODE EXTRAS...')

                    import_existing_entry_pks = {
                        entry_data[unique_identifier]: import_entry_pk
                        for import_entry_pk, entry_data in existing_entries[entity_name].items()
                    }
                    for node in session.query(DbNode).filter(DbNode.uuid.in_(import_existing_entry_pks)).all():
                        import_entry_uuid = str(node.uuid)
                        import_entry_pk = import_existing_entry_pks[import_entry_uuid]

                        if not silent:
                            # Progress bar initialization - Node
                            progress_bar.update()
                            pbar_node_base_str = pbar_base_str + 'UUID={} - '.format(import_entry_uuid.split('-')[0])
                            progress_bar.set_description_str(pbar_node_base_str + 'Extras')

                        # Get extras from import file
                        try:
                            extras = data['node_extras'][str(import_entry_pk)]
                        except KeyError:
                            raise exceptions.CorruptArchive(
                                'Unable to find extra info for Node with UUID={}'.format(import_entry_uuid)
                            )

                        old_extras = node.extras.copy()
                        # TODO: remove when aiida extras will be moved somewhere else
                        # from here
                        extras = {key: value for key, value in extras.items() if not key.startswith('_aiida_')}
                        if node.node_type.endswith('code.Code.'):
                            extras = {key: value for key, value in extras.items() if not key == 'hidden'}
                        # till here
                        new_extras = merge_extras(node.extras, extras, extras_mode_existing)
                        if new_extras != old_extras:
                            node.extras = new_extras
                            flag_modified(node, 'extras')
                            objects_to_update.append(node)

                elif not silent:
                    # Update progress bar with new non-Node entries
                    progress_bar.update(n=len(existing_entries[entity_name]) + len(new_entries[entity_name]))

                if not silent:
                    progress_bar.set_description_str(pbar_base_str + 'Storing')

                # Store them all in once; However, the PK are not set in this way...
                if objects_to_create:
                    session.add_all(objects_to_create)
                if objects_to_update:
                    session.add_all(objects_to_update)

                session.flush()

                just_saved = {}
                if import_new_entry_pks.keys():
                    if not silent:
                        reset_progress_bar = {'total': progress_bar.total, 'n': progress_bar.n}
                        progress_bar.reset(total=len(import_new_entry_pks))

                    builder = QueryBuilder()
                    builder.append(
                        entity,
                        filters={unique_identifier: {
                            'in': list(import_new_entry_pks.keys())
                        }},
                        project=[unique_identifier, 'id']
                    )

                    if silent:
                        just_saved = {v[0]: v[1] for v in builder.all()}
                    else:
                        for entry in builder.iterall():
                            progress_bar.update()

                            just_saved.update({entry[0]: entry[1]})

                if not silent:
                    progress_bar.set_description_str(pbar_base_str + 'Done!')

                # Now I have the PKs, print the info
                # Moreover, add newly created Nodes to foreign_ids_reverse_mappings
                for unique_id, new_pk in just_saved.items():
                    from uuid import UUID
                    if isinstance(unique_id, UUID):
                        unique_id = str(unique_id)
                    import_entry_pk = import_new_entry_pks[unique_id]
                    foreign_ids_reverse_mappings[entity_name][unique_id] = new_pk
                    if entity_name not in ret_dict:
                        ret_dict[entity_name] = {'new': [], 'existing': []}
                    ret_dict[entity_name]['new'].append((import_entry_pk, new_pk))

                    if debug:
                        print('NEW %s: %s (%s->%s)' % (entity_name, unique_id, import_entry_pk, new_pk))

            if debug:
                print('STORING NODE LINKS...')

            import_links = data['links_uuid']

            if not silent and import_links:
                progress_bar.reset(total=len(import_links))
                pbar_base_str = 'Links - '

            for link in import_links:
                # Check for dangling Links within the, supposed, self-consistent archive
                if not silent:
                    progress_bar.update()
                    progress_bar.set_description_str(pbar_base_str + 'label={}'.format(link['label']))

                try:
                    in_id = foreign_ids_reverse_mappings[NODE_ENTITY_NAME][link['input']]
                    out_id = foreign_ids_reverse_mappings[NODE_ENTITY_NAME][link['output']]
                except KeyError:
                    if ignore_unknown_nodes:
                        continue
                    else:
                        raise exceptions.ImportValidationError(
                            'Trying to create a link with one or both unknown nodes, stopping (in_uuid={}, '
                            'out_uuid={}, label={}, type={})'.format(
                                link['input'], link['output'], link['label'], link['type']
                            )
                        )

                # Since backend specific Links (DbLink) are not validated upon creation, we will now validate them.
                source = QueryBuilder().append(Node, filters={'id': in_id}, project='*').first()[0]
                target = QueryBuilder().append(Node, filters={'id': out_id}, project='*').first()[0]
                link_type = LinkType(link['type'])

                # Check for existence of a triple link, i.e. unique triple.
                # If it exists, then the link already exists, continue to next link, otherwise, validate link.
                if link_triple_exists(source, target, link_type, link['label']):
                    continue

                try:
                    validate_link(source, target, link_type, link['label'])
                except ValueError as why:
                    raise exceptions.ImportValidationError('Error occurred during Link validation: {}'.format(why))

                # New link
                session.add(DbLink(input_id=in_id, output_id=out_id, label=link['label'], type=link['type']))
                if 'Link' not in ret_dict:
                    ret_dict['Link'] = {'new': []}
                ret_dict['Link']['new'].append((in_id, out_id))

            if debug:
                print('   ({} new links...)'.format(len(ret_dict.get('Link', {}).get('new', []))))

            if debug:
                print('STORING GROUP ELEMENTS...')

            import_groups = data['groups_uuid']

            if not silent and import_groups:
                progress_bar.reset(total=len(import_groups))
                pbar_base_str = 'Groups - '

            for groupuuid, groupnodes in import_groups.items():
                # # TODO: cache these to avoid too many queries
                qb_group = QueryBuilder().append(Group, filters={'uuid': {'==': groupuuid}})
                group_ = qb_group.first()[0]

                if not silent:
                    progress_bar.update()
                    progress_bar.set_description_str(pbar_base_str + 'label={}'.format(group_.label))

                nodes_ids_to_add = [
                    foreign_ids_reverse_mappings[NODE_ENTITY_NAME][node_uuid] for node_uuid in groupnodes
                ]
                qb_nodes = QueryBuilder().append(Node, filters={'id': {'in': nodes_ids_to_add}})
                # Adding nodes to group avoiding the SQLA ORM to increase speed
                nodes_to_add = [n[0].backend_entity for n in qb_nodes.all()]
                group_.backend_entity.add_nodes(nodes_to_add, skip_orm=True)

            ######################################################
            # Put everything in a specific group
            ######################################################
            existing = existing_entries.get(NODE_ENTITY_NAME, {})
            existing_pk = [foreign_ids_reverse_mappings[NODE_ENTITY_NAME][v['uuid']] for v in existing.values()]
            new = new_entries.get(NODE_ENTITY_NAME, {})
            new_pk = [foreign_ids_reverse_mappings[NODE_ENTITY_NAME][v['uuid']] for v in new.values()]

            pks_for_group = existing_pk + new_pk

            # So that we do not create empty groups
            if pks_for_group:
                # If user specified a group, import all things into it
                if not group:
                    from aiida.backends.sqlalchemy.models.group import DbGroup

                    # Get an unique name for the import group, based on the current (local) time
                    basename = timezone.localtime(timezone.now()).strftime('%Y%m%d-%H%M%S')
                    counter = 0
                    group_label = basename
                    while session.query(DbGroup).filter(DbGroup.label == group_label).count() > 0:
                        counter += 1
                        group_label = '{}_{}'.format(basename, counter)

                        if counter == 100:
                            raise exceptions.ImportUniquenessError(
                                "Overflow of import groups (more than 100 import groups exists with basename '{}')"
                                ''.format(basename)
                            )
                    group = Group(label=group_label, type_string=IMPORTGROUP_TYPE)
                    session.add(group.backend_entity._dbmodel)

                # Adding nodes to group avoiding the SQLA ORM to increase speed
                builder = QueryBuilder().append(Node, filters={'id': {'in': pks_for_group}})

                if silent:
                    nodes = [entry[0].backend_entity for entry in builder.all()]
                else:
                    progress_bar.reset(total=len(pks_for_group))
                    progress_bar.set_description_str('Creating import Group - Preprocessing')
                    first = True

                    nodes = []
                    for entry in builder.iterall():
                        if first:
                            progress_bar.set_description_str('Creating import Group', refresh=False)
                            first = False
                        progress_bar.update()
                        nodes.append(entry[0].backend_entity)
                group.backend_entity.add_nodes(nodes, skip_orm=True)
            else:
                if debug:
                    print('No Nodes to import, so no Group created, if it did not already exist')

            if debug:
                print('COMMITTING EVERYTHING...')
            session.commit()

            if not silent:
                # Finalize Progress bar
                if not debug:
                    progress_bar.leave = False
                progress_bar.close()

                # Summarize import
                finish_header(ret_dict, getattr(group, 'label', None))

        except:
            if not silent:
                # Finalize Progress bar
                if not debug:
                    progress_bar.leave = False
                progress_bar.close()

                finish_header({}, None)

            if debug:
                print('Rolling back')
            session.rollback()
            raise

    return ret_dict
