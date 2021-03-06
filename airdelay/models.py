#encoding=utf-8

from __future__ import print_function, unicode_literals

import sqlalchemy
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import sessionmaker


Base = declarative_base()
engine = sqlalchemy.create_engine('postgresql://airdelay:airdelay@localhost:5432/airdelay-dev', echo=False)
Session = sessionmaker(bind=engine)


class Airport(Base):
    __tablename__ = 'airports'
    id = Column(Integer, primary_key=True)
    iata = Column(String(4))
    name = Column(String)

    def table(self, start=None, end=None):
        filters = []
        if start:
            filters.append(lambda o: o.created_at >= start)
        if end:
            filters.append(lambda o: o.created_at <= end)
        def check_filters(obj):
            return min([f(obj) for f in filters])
        for flight in self.flight_set.all():
            if not check_filters(flight):
                continue
            print('''\
{f.code:<30} | {f.created_at:%d.%m.%Y %H:%M} | {f.scheduled:%d.%m.%Y %H:%M} | {f.actual:%d.%m.%Y %H:%M}\
            '''.format(f=flight))

    def __unicode__(self):
        return u'{}'.format(self.iata)

    def __repr__(self):
        return '<Airport: {}>'.format(self.iata)


class StatusBase(object):
    _list = []

    @classmethod
    def lend_to_class(cls, klass, field='status'):
        for status in cls._list:
            property_name = 'is_{}'.format(status.lower())
            status_value = getattr(cls, status)
            def setx(self, value, _status=status_value):
                assert isinstance(value, bool)
                if value:
                    setattr(self, field, _status)
                else:
                    setattr(self, field, None)
            def getx(self, _status=status_value):
                return getattr(self, field) == _status
            setattr(klass, property_name, property(getx, setx))
        setattr(klass, field + '_list', cls)


class FlightStatus(StatusBase):
    _list = 'SCHEDULED', 'DELAYED', 'DEPARTED', 'LANDED', 'CANCELLED'

    SCHEDULED = 10
    DELAYED = 20
    DEPARTED = 30
    LANDED = 35
    CANCELLED = 40


class FlightType(StatusBase):
    _list = 'INBOUND', 'OUTBOUND'

    INBOUND = -1
    OUTBOUND = 1


class Flight(Base):
    code = Column(required=True)
    airport = models.ReferenceField(Airport, required=True)
    peer_airport_name = models.Attribute(required=True)
    type = models.IntegerField(required=True)
    created_at = models.DateTimeField(auto_now_add=True)
    scheduled = models.DateTimeField(required=True)
    actual = models.DateTimeField(required=True)
    status = models.IntegerField(required=True)
    delay_minutes = models.IntegerField()
    codeshare = models.IntegerField(default=0)

    ONTIME_WEIGHT = -15
    DELAY_WEIGHT = 10
    DELAY_UNIT = 15

    def save(self):
        self.delay_minutes = int((self.actual - self.scheduled).total_seconds() / 60)
        super(Flight, self).save()

    def __unicode__(self):
        return unicode('Flight {}'.format(self.code))

    @property
    def created_at_compressed(self):
        return self.created_at.replace(
            minute=self.created_at.minute / 10 * 10,
            second=0,
            microsecond=0
        )

    @property
    def delay_weight(self):
        if self.delay_minutes == 0:
            return self.ONTIME_WEIGHT
        else:
            return abs(
                self.delay_minutes - self.DELAY_UNIT
            ) / self.DELAY_UNIT * self.DELAY_WEIGHT

    def get_csv(self):
        values = []
        fields = [f.name for f in self.fields]
        for field in fields:
            values.append(unicode(getattr(self, field)))
        return fields, ','.join(values)

    class Meta:
#        indices = ('full_name',)
        db = redis.Redis(host='localhost', db=9)


FlightStatus.lend_to_class(Flight)
FlightType.lend_to_class(Flight, 'type')
