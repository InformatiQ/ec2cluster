import json
import boto
from boto.utils import get_instance_userdata, get_instance_metadata
from boto.route53.record import ResourceRecordSets
import dns
import dns.resolver
import os
import subprocess
import psycopg2
import logging
from crontab import CronTab

from ec2cluster import default_settings as settings


class EC2Mixin(object):
    def get_metadata(self):
        data = get_instance_metadata()
        data.update(json.loads(get_instance_userdata()))
        return data

    def _get_route53_conn(self):
        return boto.connect_route53(aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY)

    def acquire_master_cname(self, force=False):
        # TODO move this to EC2Mixin after initial testing
        """ Use Route53 to update the master_cname record to point to this instance.

            If the CNAME already exists and force is False, an exception will be raised.
            Setting force to True will cause this function to 'take' the DNS record.
        """
        try:
            answers = dns.resolver.query(self.master_cname, 'CNAME')
        except dns.resolver.NXDOMAIN:
            master_cname_exists = False
            self.logger.info('%s does not exist, so creating it' % self.master_cname)
        else:
            master_cname_exists = True
            old_cname_value = answers.rrset.items[0].to_text()
            self.logger.info('%s already exists, so updating it' % self.master_cname)

        if master_cname_exists == True and force == False:
            self.logger.critical('CNAME %s exists and force is false - exiting' % self.master_cname)
            raise Exception('CNAME %s exists and force is False - not taking the CNAME' % self.master_cname)

        # if we get here, either the CNAME does not exist or Force is true, so we should take the CNAME
        route53_conn = self._get_route53_conn()

        changes = ResourceRecordSets(route53_conn, settings.ROUTE53_ZONE_ID)
        if master_cname_exists:
            self.logger.info('Deleting existing record for %s' % self.master_cname)
            del_record = changes.add_change('DELETE', self.master_cname, 'CNAME', ttl=settings.MASTER_CNAME_TTL)
            del_record.add_value(old_cname_value)

        self.logger.info('Creating record for %s' % self.master_cname)
        add_record = changes.add_change('CREATE', self.master_cname, 'CNAME', ttl=settings.MASTER_CNAME_TTL)
        add_record.add_value(self.metadata['public-hostname'])
        changes.commit()
        self.logger.info('Finished updating DNS records')

    def add_to_slave_cname_pool(self):
        """ Add this instance to the pool of hostnames for slave.<cluster name>.goteam.be.

            This is a pool of 'weighted resource recordsets', which allows traffic to be distributed to
            multiple read-slaves.
        """
        route53_conn = self._get_route53_conn()

        changes = ResourceRecordSets(route53_conn, settings.ROUTE53_ZONE_ID)

        self.logger.info('Adding %s to CNAME pool for %s' % (self.metadata['instance-id'], self.slave_cname))
        add_record = changes.add_change('CREATE',
            self.slave_cname,
            'CNAME',
            ttl=settings.SLAVE_CNAME_TTL,
            weight='10',
            identifier=self.metadata['instance-id'])
        add_record.add_value(self.metadata['public-hostname'])
        try:
            changes.commit()
        except boto.route53.exception.DNSServerError, e:
            if e.error_message.endswith('it already exists'):
                # This instance is already in the pool - carry on as normal.
                self.logger.warning('Attempted to create a CNAME, but one already exists for this instance')
            else:
                raise
        self.logger.info('Finished updating DNS records')


class VagrantMixin(object):
    def get_metadata(self):
        data = os.environ
        data['cluster'] = 'vagranttest'
        data['public-hostname'] = 'instance12346.vagranttest.example.com'
        data['instance-id'] = 'i-12346'
        return data


def get_cluster_class(infrastructureClass, serviceClass):
    clusterClass = type('className', (serviceClass, infrastructureClass), {})
    return clusterClass


class BaseCluster(object):
    """ Base class for generic master/slave operations.
    """

    MASTER = 'master'
    SLAVE = 'slave'
    POLL_TIMEOUT = 60

    def get_metadata(self):
        raise NotImplementedError

    def __init__(self, settings=None):
        self.logger = logging.getLogger('%s.%s' % (__name__, self.__class__.__name__))
        self.logger.warning('test')

        if settings is None:
            settings = {}
        self.settings = settings
        self.metadata = self.get_metadata()
        self.master_cname = self.get_master_cname()
        self.slave_cname = self.get_slave_cname()
        self.roles = self.get_roles()

    def get_roles(self):
        return {
            self.MASTER: self.prepare_master,
            self.SLAVE: self.prepare_slave
        }

    def initialise(self):
        """ Initialises this server as a master or slave.
        """
        self.role = self.determine_role()
        if self.role in self.roles:
            # Call the function for this role, as declared in get_roles().
            self.roles[self.role]()
        else:
            self.logger.critical('Unknown role: %s' % self.role)
            raise Exception('Unrecognised role: %s' % self.role)
        self.start_process()
        # Poll the process until it either starts successfully, or fails. This will result
        # in a call to process_started or process_failed.
        self.poll_process()

    def get_master_cname(self):
        """ Returns the CNAME of the master server for this cluster.
        """
        return settings.MASTER_CNAME % self.metadata

    def get_slave_cname(self):
        """ Returns the CNAME of the slave servers for this cluster.
        """
        return settings.SLAVE_CNAME % self.metadata

    def determine_role(self):
        """ Should we be a master or a slave?

            If the self.master_cname DNS record exists, we should be a slave.
        """
        self.logger.info('Attempting to determine role')
        try:
            dns.resolver.query(self.master_cname, 'CNAME')
        except dns.resolver.NXDOMAIN:
            self.logger.info('Master CNAME does not exist, assuming master role')
            return self.MASTER
        else:
            self.logger.info('Master CNAME already exists, assuming slave role')
            return self.SLAVE

    def prepare_master(self):
        """ Initialise the master server.
        """
        raise NotImplementedError

    def prepare_slave(self):
        """ Initialise the slave server.
        """
        raise NotImplementedError

    def acquire_master_cname(self):
        """ Updates the master CNAME to point to this instance's public DNS name.
        """
        raise NotImplementedError

    def release_master_cname(self):
        """ Deletes the master CNAME if it is pointing to this instance. Called when
            the master process fails to start.
        """
        raise NotImplementedError

    def poll_process(self):
        raise NotImplementedError

    def start_process(self):
        """ Attempt to start the process (e.g. via supervisorctl).
        """
        raise NotImplementedError

    def process_started(self):
        if self.role == self.MASTER:
            self.acquire_master_cname()
        elif self.role == self.SLAVE:
            self.add_to_slave_cname_pool()

    def process_failed(self):
        print 'oh shit something broke'


# TODO settings
SERVICE_NAME = 'testservice'
MASTER_SCRIPT = '/tmp/master.py'
SLAVE_SCRIPT = '/tmp/slave.py'


class ScriptCluster(BaseCluster):
    """ Basic cluster - simply runs scripts when preparing a master/slave, and
        starts a service via init.d scripts.
    """
    def start_process(self):
        subprocess.check_call(['/etc/init.d/%s' % SERVICE_NAME, 'start'])

    def prepare_master(self):
        subprocess.check_call([MASTER_SCRIPT, ])

    def prepare_slave(self):
        subprocess.check_call([SLAVE_SCRIPT, ])

    def poll_process(self):
        self.process_started()


class PostgresqlCluster(EC2Mixin, BaseCluster):
    """ PostgreSQL cluster.

        Master: Starts postgres normally
        Slave: Writes a recovery.conf file and starts postgres as a read slave

        The prepare_[master|slave] functions will put the instance in a state whereby
        '/etc/init.d/postgresql start' can be executed.
    """
    def _get_conn(self, host=None, dbname=None, user=None):
        """ Returns a connection to postgresql server.
        """
        conn_str = ''
        if host:
            conn_str += 'host=%s ' % host
        if dbname:
            conn_str += 'dbname=%s ' % dbname
        if user:
            conn_str += 'user=%s ' % user

        return psycopg2.connect(conn_str)

    def start_process(self):
        """ Starts postgresql using the init.d scripts.
        """
        subprocess.check_call(['/etc/init.d/postgresql', 'start'])

    def poll_process(self):
        pass

    def write_recovery_conf(self, template_path):
        """ Using the template specified in settings, create a recovery.conf file in the
            postgres config dir.
        """
        self.logger.info('Writing recovery file using template %s' % template_path)
        data = dict(self.metadata.items() + self.settings.items())
        data.update(
            {'master_cname': self.master_cname}
        )
        template_file = open(template_path, 'r')
        template = template_file.read()
        template_file.close()
        output = open(settings.RECOVERY_FILENAME, 'w')
        output.write(template % data)
        output.close()

    def configure_cron_backup(self):
        """ Creates a cronjob to perform backups via snaptastic.

            Default behaviour is to take backups at 08:00 each day.
        """
        backup_cmd = 'PATH=/usr/local/bin:/usr/sbin snaptastic make-snapshots postgresql'

        cron = CronTab('postgres')

        # check if this job already exists - this is not perfect, but should stop us from running
        # the exact same multiple times simultaneously
        for job in cron:
            if job.command.command() == backup_cmd:
                self.logger.warning('The backup cron job already exists - skipping')
                return

        backup_job = cron.new(command=backup_cmd, comment='Created by ec2cluster')
        backup_job.hour.every(8)
        self.logger.info('Adding entry to postgres users crontab - %s' % backup_cmd)
        cron.write()

    def prepare_master(self):
        """ Init postgres as a master.
        """
        # TODO remove acquire call, this happens after proc is started
        self.acquire_master_cname()
        self.write_recovery_conf(settings.RECOVERY_TEMPLATE_MASTER)
        self.configure_cron_backup()
        # TODO apply some tags here to show the role of the instance

    def prepare_slave(self):
        """ Init postgres as a read-slave by writing a recovery.conf file.
        """
        self.write_recovery_conf(settings.RECOVERY_TEMPLATE_SLAVE)
        self.logger.info('Instance configured as a slave')
        # TODO remove cname call, this happens after proc is started
        self.add_to_slave_cname_pool()
        # TODO apply some tags here to show the role of the instance


    def check_master(self):
        """ Returns true if there is a postgresql server running on the master CNAME
            for this cluster, and this instance believes it is the master.
            This is a safety check to avoid promoting a slave when we already have a
            master in the cluster.
        """
        # TODO untested
        self.logger.info('Checking master DB at %s' % self.master_cname)
        try:
            conn = self._get_conn(host=self.master_cname)
        except psycopg2.OperationalError:
            self.logger.info('Connecting to master failed')
            return False

        cur = conn.cursor()
        cur.execute('SELECT pg_is_in_recovery()')
        res = cur.fetchone()
        if res == 't':
            # We server we connected to thinks it is a slave
            return False

        # Perform a basic query to make sure postgresql is operational
        cur.execute('SELECT 1')
        res = cur.fetchone()
        if res == '1':
            return True

        # If we get here, something went wrong
        return False

    def check_slave(self):
        """ Returns true if there is a postgresql server running on localhost, and
            the server is in recovery mode (i.e. it is a read slave).
        """
        # TODO untested
        self.logger.info('Checking slave DB on localhost')
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute('SELECT pg_is_in_recovery()')
        res = cur.fetchone()
        # TODO result won't be a plain string
        return (res == 't')

    def promote(self, force=False):
        """ Promote a read-slave to the master role.

            If force is True, safety checks are ignored and the promotion is forced.
        """
        try:
            active_master = self.check_master()
        except psycopg2.OperationalError, e:
            print 'Could not connect to master'
            active_master = False

        if active_master == True:
            print 'There is an active server at %s' % self.master_cname
            if force == False:
                print 'Refusing to promote slave without "force", exiting.'
                return
        promote_cmd = '%(pg_ctl)s -D %(dir)s promote' % {
            'user': settings.PG_USER,
            'pg_ctl': settings.PG_CTL,
            'dir': settings.PG_DIR}
        print 'Running promote command: %s' % promote_cmd
        # TODO error checking, log output
        try:
            subprocess.check_output(promote_cmd.split(),
                stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError, e:
            if e.output.endswith('server is not in standby mode\n'):
                self.logger.critical('This server is not in standby mode, so can not be promoted')
                # TODO custom exception?
                raise Exception(e.output)
            else:
                print e.output
                raise e

        # If we get here, then postgresql should have been successfully promoted.

        # Let's start doing backups
        # TODO do we need to clean the backup here? new basebackup or something?
        self.configure_cron_backup()
