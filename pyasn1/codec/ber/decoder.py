#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2019, Ilya Etingof <etingof@gmail.com>
# License: http://snmplabs.com/pyasn1/license.html
#
import os

from pyasn1 import debug
from pyasn1 import error
from pyasn1.codec.ber import eoo
from pyasn1.codec.streaming import asSeekableStream
from pyasn1.codec.streaming import isEndOfStream
from pyasn1.codec.streaming import peekIntoStream
from pyasn1.codec.streaming import readFromStream
from pyasn1.compat.integer import from_bytes
from pyasn1.compat.octets import oct2int, octs2ints, ints2octs, null
from pyasn1.error import PyAsn1Error
from pyasn1.type import base
from pyasn1.type import char
from pyasn1.type import tag
from pyasn1.type import tagmap
from pyasn1.type import univ
from pyasn1.type import useful

__all__ = ['StreamingDecoder', 'Decoder', 'decode']

LOG = debug.registerLoggee(__name__, flags=debug.DEBUG_DECODER)

noValue = base.noValue

SubstrateUnderrunError = error.SubstrateUnderrunError


class AbstractPayloadDecoder(object):
    protoComponent = None

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):
        """Decode value with fixed byte length.

        The decoder is allowed to consume as many bytes as necessary.
        """
        raise error.PyAsn1Error('SingleItemDecoder not implemented for %s' % (tagSet,))  # TODO: Seems more like an NotImplementedError?

    def indefLenValueDecoder(self, substrate, asn1Spec,
                             tagSet=None, length=None, state=None,
                             decodeFun=None, substrateFun=None,
                             **options):
        """Decode value with undefined length.

        The decoder is allowed to consume as many bytes as necessary.
        """
        raise error.PyAsn1Error('Indefinite length mode decoder not implemented for %s' % (tagSet,)) # TODO: Seems more like an NotImplementedError?

    @staticmethod
    def _passAsn1Object(asn1Object, options):
        if 'asn1Object' not in options:
            options['asn1Object'] = asn1Object

        return options


class AbstractSimplePayloadDecoder(AbstractPayloadDecoder):
    @staticmethod
    def substrateCollector(asn1Object, substrate, length, options):
        for chunk in readFromStream(substrate, length, options):
            yield chunk

    def _createComponent(self, asn1Spec, tagSet, value, **options):
        if options.get('native'):
            return value
        elif asn1Spec is None:
            return self.protoComponent.clone(value, tagSet=tagSet)
        elif value is noValue:
            return asn1Spec
        else:
            return asn1Spec.clone(value)


class RawPayloadDecoder(AbstractSimplePayloadDecoder):
    protoComponent = univ.Any('')

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):
        if substrateFun:
            asn1Object = self._createComponent(asn1Spec, tagSet, '', **options)

            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        for value in decodeFun(substrate, asn1Spec, tagSet, length, **options):
            yield value

    def indefLenValueDecoder(self, substrate, asn1Spec,
                             tagSet=None, length=None, state=None,
                             decodeFun=None, substrateFun=None,
                             **options):
        if substrateFun:
            asn1Object = self._createComponent(asn1Spec, tagSet, '', **options)

            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        while True:
            for value in decodeFun(
                    substrate, asn1Spec, tagSet, length,
                    allowEoo=True, **options):

                if value is eoo.endOfOctets:
                    return

                yield value


rawPayloadDecoder = RawPayloadDecoder()


class IntegerPayloadDecoder(AbstractSimplePayloadDecoder):
    protoComponent = univ.Integer(0)

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):

        if tagSet[0].tagFormat != tag.tagFormatSimple:
            raise error.PyAsn1Error('Simple tag format expected')

        for chunk in readFromStream(substrate, length, options):
            if isinstance(chunk, SubstrateUnderrunError):
                yield chunk

        if chunk:
            value = from_bytes(chunk, signed=True)

        else:
            value = 0

        yield self._createComponent(asn1Spec, tagSet, value, **options)


class BooleanPayloadDecoder(IntegerPayloadDecoder):
    protoComponent = univ.Boolean(0)

    def _createComponent(self, asn1Spec, tagSet, value, **options):
        return IntegerPayloadDecoder._createComponent(
            self, asn1Spec, tagSet, value and 1 or 0, **options)


class BitStringPayloadDecoder(AbstractSimplePayloadDecoder):
    protoComponent = univ.BitString(())
    supportConstructedForm = True

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):

        if substrateFun:
            asn1Object = self._createComponent(asn1Spec, tagSet, noValue, **options)

            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        if not length:
            raise error.PyAsn1Error('Empty BIT STRING substrate')

        for chunk in isEndOfStream(substrate):
            if isinstance(chunk, SubstrateUnderrunError):
                yield chunk

        if chunk:
            raise error.PyAsn1Error('Empty BIT STRING substrate')

        if tagSet[0].tagFormat == tag.tagFormatSimple:  # XXX what tag to check?

            for trailingBits in readFromStream(substrate, 1, options):
                if isinstance(trailingBits, SubstrateUnderrunError):
                    yield trailingBits

            trailingBits = ord(trailingBits)
            if trailingBits > 7:
                raise error.PyAsn1Error(
                    'Trailing bits overflow %s' % trailingBits
                )

            for chunk in readFromStream(substrate, length - 1, options):
                if isinstance(chunk, SubstrateUnderrunError):
                    yield chunk

            value = self.protoComponent.fromOctetString(
                chunk, internalFormat=True, padding=trailingBits)

            yield self._createComponent(asn1Spec, tagSet, value, **options)

            return

        if not self.supportConstructedForm:
            raise error.PyAsn1Error('Constructed encoding form prohibited '
                                    'at %s' % self.__class__.__name__)

        if LOG:
            LOG('assembling constructed serialization')

        # All inner fragments are of the same type, treat them as octet string
        substrateFun = self.substrateCollector

        bitString = self.protoComponent.fromOctetString(null, internalFormat=True)

        current_position = substrate.tell()

        while substrate.tell() - current_position < length:
            for component in decodeFun(
                    substrate, self.protoComponent, substrateFun=substrateFun,
                    **options):
                if isinstance(component, SubstrateUnderrunError):
                    yield component

            trailingBits = oct2int(component[0])
            if trailingBits > 7:
                raise error.PyAsn1Error(
                    'Trailing bits overflow %s' % trailingBits
                )

            bitString = self.protoComponent.fromOctetString(
                component[1:], internalFormat=True,
                prepend=bitString, padding=trailingBits
            )

        yield self._createComponent(asn1Spec, tagSet, bitString, **options)

    def indefLenValueDecoder(self, substrate, asn1Spec,
                             tagSet=None, length=None, state=None,
                             decodeFun=None, substrateFun=None,
                             **options):

        if substrateFun:
            asn1Object = self._createComponent(asn1Spec, tagSet, noValue, **options)

            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        # All inner fragments are of the same type, treat them as octet string
        substrateFun = self.substrateCollector

        bitString = self.protoComponent.fromOctetString(null, internalFormat=True)

        while True:  # loop over fragments

            for component in decodeFun(
                    substrate, self.protoComponent, substrateFun=substrateFun,
                    allowEoo=True, **options):

                if component is eoo.endOfOctets:
                    break

                if isinstance(component, SubstrateUnderrunError):
                    yield component

            if component is eoo.endOfOctets:
                break

            trailingBits = oct2int(component[0])
            if trailingBits > 7:
                raise error.PyAsn1Error(
                    'Trailing bits overflow %s' % trailingBits
                )

            bitString = self.protoComponent.fromOctetString(
                component[1:], internalFormat=True,
                prepend=bitString, padding=trailingBits
            )

        yield self._createComponent(asn1Spec, tagSet, bitString, **options)


class OctetStringPayloadDecoder(AbstractSimplePayloadDecoder):
    protoComponent = univ.OctetString('')
    supportConstructedForm = True

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):
        if substrateFun:
            asn1Object = self._createComponent(asn1Spec, tagSet, noValue, **options)

            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        if tagSet[0].tagFormat == tag.tagFormatSimple:  # XXX what tag to check?
            for chunk in readFromStream(substrate, length, options):
                if isinstance(chunk, SubstrateUnderrunError):
                    yield chunk

            yield self._createComponent(asn1Spec, tagSet, chunk, **options)

            return

        if not self.supportConstructedForm:
            raise error.PyAsn1Error('Constructed encoding form prohibited at %s' % self.__class__.__name__)

        if LOG:
            LOG('assembling constructed serialization')

        # All inner fragments are of the same type, treat them as octet string
        substrateFun = self.substrateCollector

        header = null

        original_position = substrate.tell()
        # head = popSubstream(substrate, length)
        while substrate.tell() - original_position < length:
            for component in decodeFun(
                    substrate, self.protoComponent, substrateFun=substrateFun,
                    **options):
                if isinstance(component, SubstrateUnderrunError):
                    yield component

            header += component

        yield self._createComponent(asn1Spec, tagSet, header, **options)

    def indefLenValueDecoder(self, substrate, asn1Spec,
                             tagSet=None, length=None, state=None,
                             decodeFun=None, substrateFun=None,
                             **options):
        if substrateFun and substrateFun is not self.substrateCollector:
            asn1Object = self._createComponent(asn1Spec, tagSet, noValue, **options)

            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        # All inner fragments are of the same type, treat them as octet string
        substrateFun = self.substrateCollector

        header = null

        while True:  # loop over fragments

            for component in decodeFun(
                    substrate, self.protoComponent, substrateFun=substrateFun,
                    allowEoo=True, **options):

                if isinstance(component, SubstrateUnderrunError):
                    yield component

                if component is eoo.endOfOctets:
                    break

            if component is eoo.endOfOctets:
                break

            header += component

        yield self._createComponent(asn1Spec, tagSet, header, **options)


class NullPayloadDecoder(AbstractSimplePayloadDecoder):
    protoComponent = univ.Null('')

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):

        if tagSet[0].tagFormat != tag.tagFormatSimple:
            raise error.PyAsn1Error('Simple tag format expected')

        for chunk in readFromStream(substrate, length, options):
            if isinstance(chunk, SubstrateUnderrunError):
                yield chunk

        component = self._createComponent(asn1Spec, tagSet, '', **options)

        if chunk:
            raise error.PyAsn1Error('Unexpected %d-octet substrate for Null' % length)

        yield component


class ObjectIdentifierPayloadDecoder(AbstractSimplePayloadDecoder):
    protoComponent = univ.ObjectIdentifier(())

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):
        if tagSet[0].tagFormat != tag.tagFormatSimple:
            raise error.PyAsn1Error('Simple tag format expected')

        for chunk in readFromStream(substrate, length, options):
            if isinstance(chunk, SubstrateUnderrunError):
                yield chunk

        if not chunk:
            raise error.PyAsn1Error('Empty substrate')

        chunk = octs2ints(chunk)

        oid = ()
        index = 0
        substrateLen = len(chunk)
        while index < substrateLen:
            subId = chunk[index]
            index += 1
            if subId < 128:
                oid += (subId,)
            elif subId > 128:
                # Construct subid from a number of octets
                nextSubId = subId
                subId = 0
                while nextSubId >= 128:
                    subId = (subId << 7) + (nextSubId & 0x7F)
                    if index >= substrateLen:
                        raise error.SubstrateUnderrunError(
                            'Short substrate for sub-OID past %s' % (oid,)
                        )
                    nextSubId = chunk[index]
                    index += 1
                oid += ((subId << 7) + nextSubId,)
            elif subId == 128:
                # ASN.1 spec forbids leading zeros (0x80) in OID
                # encoding, tolerating it opens a vulnerability. See
                # https://www.esat.kuleuven.be/cosic/publications/article-1432.pdf
                # page 7
                raise error.PyAsn1Error('Invalid octet 0x80 in OID encoding')

        # Decode two leading arcs
        if 0 <= oid[0] <= 39:
            oid = (0,) + oid
        elif 40 <= oid[0] <= 79:
            oid = (1, oid[0] - 40) + oid[1:]
        elif oid[0] >= 80:
            oid = (2, oid[0] - 80) + oid[1:]
        else:
            raise error.PyAsn1Error('Malformed first OID octet: %s' % chunk[0])

        yield self._createComponent(asn1Spec, tagSet, oid, **options)


class RealPayloadDecoder(AbstractSimplePayloadDecoder):
    protoComponent = univ.Real()

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):
        if tagSet[0].tagFormat != tag.tagFormatSimple:
            raise error.PyAsn1Error('Simple tag format expected')

        for chunk in readFromStream(substrate, length, options):
            if isinstance(chunk, SubstrateUnderrunError):
                yield chunk

        if not chunk:
            yield self._createComponent(asn1Spec, tagSet, 0.0, **options)
            return

        fo = oct2int(chunk[0])
        chunk = chunk[1:]
        if fo & 0x80:  # binary encoding
            if not chunk:
                raise error.PyAsn1Error("Incomplete floating-point value")

            if LOG:
                LOG('decoding binary encoded REAL')

            n = (fo & 0x03) + 1

            if n == 4:
                n = oct2int(chunk[0])
                chunk = chunk[1:]

            eo, chunk = chunk[:n], chunk[n:]

            if not eo or not chunk:
                raise error.PyAsn1Error('Real exponent screwed')

            e = oct2int(eo[0]) & 0x80 and -1 or 0

            while eo:  # exponent
                e <<= 8
                e |= oct2int(eo[0])
                eo = eo[1:]

            b = fo >> 4 & 0x03  # base bits

            if b > 2:
                raise error.PyAsn1Error('Illegal Real base')

            if b == 1:  # encbase = 8
                e *= 3

            elif b == 2:  # encbase = 16
                e *= 4
            p = 0

            while chunk:  # value
                p <<= 8
                p |= oct2int(chunk[0])
                chunk = chunk[1:]

            if fo & 0x40:  # sign bit
                p = -p

            sf = fo >> 2 & 0x03  # scale bits
            p *= 2 ** sf
            value = (p, 2, e)

        elif fo & 0x40:  # infinite value
            if LOG:
                LOG('decoding infinite REAL')

            value = fo & 0x01 and '-inf' or 'inf'

        elif fo & 0xc0 == 0:  # character encoding
            if not chunk:
                raise error.PyAsn1Error("Incomplete floating-point value")

            if LOG:
                LOG('decoding character encoded REAL')

            try:
                if fo & 0x3 == 0x1:  # NR1
                    value = (int(chunk), 10, 0)

                elif fo & 0x3 == 0x2:  # NR2
                    value = float(chunk)

                elif fo & 0x3 == 0x3:  # NR3
                    value = float(chunk)

                else:
                    raise error.SubstrateUnderrunError(
                        'Unknown NR (tag %s)' % fo
                    )

            except ValueError:
                raise error.SubstrateUnderrunError(
                    'Bad character Real syntax'
                )

        else:
            raise error.SubstrateUnderrunError(
                'Unknown encoding (tag %s)' % fo
            )

        yield self._createComponent(asn1Spec, tagSet, value, **options)


class AbstractConstructedPayloadDecoder(AbstractPayloadDecoder):
    protoComponent = None


class ConstructedPayloadDecoderBase(AbstractConstructedPayloadDecoder):
    protoRecordComponent = None
    protoSequenceComponent = None

    def _getComponentTagMap(self, asn1Object, idx):
        raise NotImplementedError()

    def _getComponentPositionByType(self, asn1Object, tagSet, idx):
        raise NotImplementedError()

    def _decodeComponentsSchemaless(
            self, substrate, tagSet=None, decodeFun=None,
            length=None, **options):

        asn1Object = None

        components = []
        componentTypes = set()

        original_position = substrate.tell()

        while length == -1 or substrate.tell() < original_position + length:
            for component in decodeFun(substrate, **options):
                if isinstance(component, SubstrateUnderrunError):
                    yield component

            if length == -1 and component is eoo.endOfOctets:
                break

            components.append(component)
            componentTypes.add(component.tagSet)

            # Now we have to guess is it SEQUENCE/SET or SEQUENCE OF/SET OF
            # The heuristics is:
            # * 1+ components of different types -> likely SEQUENCE/SET
            # * otherwise -> likely SEQUENCE OF/SET OF
            if len(componentTypes) > 1:
                protoComponent = self.protoRecordComponent

            else:
                protoComponent = self.protoSequenceComponent

            asn1Object = protoComponent.clone(
                # construct tagSet from base tag from prototype ASN.1 object
                # and additional tags recovered from the substrate
                tagSet=tag.TagSet(protoComponent.tagSet.baseTag, *tagSet.superTags)
            )

        if LOG:
            LOG('guessed %r container type (pass `asn1Spec` to guide the '
                'decoder)' % asn1Object)

        for idx, component in enumerate(components):
            asn1Object.setComponentByPosition(
                idx, component,
                verifyConstraints=False,
                matchTags=False, matchConstraints=False
            )

        yield asn1Object

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):
        if tagSet[0].tagFormat != tag.tagFormatConstructed:
            raise error.PyAsn1Error('Constructed tag format expected')

        original_position = substrate.tell()

        if substrateFun:
            if asn1Spec is not None:
                asn1Object = asn1Spec.clone()

            elif self.protoComponent is not None:
                asn1Object = self.protoComponent.clone(tagSet=tagSet)

            else:
                asn1Object = self.protoRecordComponent, self.protoSequenceComponent

            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        if asn1Spec is None:
            for asn1Object in self._decodeComponentsSchemaless(
                    substrate, tagSet=tagSet, decodeFun=decodeFun,
                    length=length, **options):
                if isinstance(asn1Object, SubstrateUnderrunError):
                    yield asn1Object

            if substrate.tell() < original_position + length:
                if LOG:
                    for trailing in readFromStream(substrate, context=options):
                        if isinstance(trailing, SubstrateUnderrunError):
                            yield trailing

                    LOG('Unused trailing %d octets encountered: %s' % (
                        len(trailing), debug.hexdump(trailing)))

            yield asn1Object

            return

        asn1Object = asn1Spec.clone()
        asn1Object.clear()

        options = self._passAsn1Object(asn1Object, options)

        if asn1Spec.typeId in (univ.Sequence.typeId, univ.Set.typeId):

            namedTypes = asn1Spec.componentType

            isSetType = asn1Spec.typeId == univ.Set.typeId
            isDeterministic = not isSetType and not namedTypes.hasOptionalOrDefault

            if LOG:
                LOG('decoding %sdeterministic %s type %r chosen by type ID' % (
                    not isDeterministic and 'non-' or '', isSetType and 'SET' or '',
                    asn1Spec))

            seenIndices = set()
            idx = 0
            while substrate.tell() - original_position < length:
                if not namedTypes:
                    componentType = None

                elif isSetType:
                    componentType = namedTypes.tagMapUnique

                else:
                    try:
                        if isDeterministic:
                            componentType = namedTypes[idx].asn1Object

                        elif namedTypes[idx].isOptional or namedTypes[idx].isDefaulted:
                            componentType = namedTypes.getTagMapNearPosition(idx)

                        else:
                            componentType = namedTypes[idx].asn1Object

                    except IndexError:
                        raise error.PyAsn1Error(
                            'Excessive components decoded at %r' % (asn1Spec,)
                        )

                for component in decodeFun(substrate, componentType, **options):
                    if isinstance(component, SubstrateUnderrunError):
                        yield component

                if not isDeterministic and namedTypes:
                    if isSetType:
                        idx = namedTypes.getPositionByType(component.effectiveTagSet)

                    elif namedTypes[idx].isOptional or namedTypes[idx].isDefaulted:
                        idx = namedTypes.getPositionNearType(component.effectiveTagSet, idx)

                asn1Object.setComponentByPosition(
                    idx, component,
                    verifyConstraints=False,
                    matchTags=False, matchConstraints=False
                )

                seenIndices.add(idx)
                idx += 1

            if LOG:
                LOG('seen component indices %s' % seenIndices)

            if namedTypes:
                if not namedTypes.requiredComponents.issubset(seenIndices):
                    raise error.PyAsn1Error(
                        'ASN.1 object %s has uninitialized '
                        'components' % asn1Object.__class__.__name__)

                if  namedTypes.hasOpenTypes:

                    openTypes = options.get('openTypes', {})

                    if LOG:
                        LOG('user-specified open types map:')

                        for k, v in openTypes.items():
                            LOG('%s -> %r' % (k, v))

                    if openTypes or options.get('decodeOpenTypes', False):

                        for idx, namedType in enumerate(namedTypes.namedTypes):
                            if not namedType.openType:
                                continue

                            if namedType.isOptional and not asn1Object.getComponentByPosition(idx).isValue:
                                continue

                            governingValue = asn1Object.getComponentByName(
                                namedType.openType.name
                            )

                            try:
                                openType = openTypes[governingValue]

                            except KeyError:

                                if LOG:
                                    LOG('default open types map of component '
                                        '"%s.%s" governed by component "%s.%s"'
                                        ':' % (asn1Object.__class__.__name__,
                                               namedType.name,
                                               asn1Object.__class__.__name__,
                                               namedType.openType.name))

                                    for k, v in namedType.openType.items():
                                        LOG('%s -> %r' % (k, v))

                                try:
                                    openType = namedType.openType[governingValue]

                                except KeyError:
                                    if LOG:
                                        LOG('failed to resolve open type by governing '
                                            'value %r' % (governingValue,))
                                    continue

                            if LOG:
                                LOG('resolved open type %r by governing '
                                    'value %r' % (openType, governingValue))

                            containerValue = asn1Object.getComponentByPosition(idx)

                            if containerValue.typeId in (
                                    univ.SetOf.typeId, univ.SequenceOf.typeId):

                                for pos, containerElement in enumerate(
                                        containerValue):

                                    stream = asSeekableStream(containerValue[pos].asOctets())

                                    for component in decodeFun(stream, asn1Spec=openType, **options):
                                        if isinstance(component, SubstrateUnderrunError):
                                            yield component

                                    containerValue[pos] = component

                            else:
                                stream = asSeekableStream(asn1Object.getComponentByPosition(idx).asOctets())

                                for component in decodeFun(stream, asn1Spec=openType, **options):
                                    if isinstance(component, SubstrateUnderrunError):
                                        yield component

                                asn1Object.setComponentByPosition(idx, component)

            else:
                inconsistency = asn1Object.isInconsistent
                if inconsistency:
                    raise inconsistency

        else:
            componentType = asn1Spec.componentType

            if LOG:
                LOG('decoding type %r chosen by given `asn1Spec`' % componentType)

            idx = 0

            while substrate.tell() - original_position < length:
                for component in decodeFun(substrate, componentType, **options):
                    if isinstance(component, SubstrateUnderrunError):
                        yield component

                asn1Object.setComponentByPosition(
                    idx, component,
                    verifyConstraints=False,
                    matchTags=False, matchConstraints=False
                )

                idx += 1

        yield asn1Object

    def indefLenValueDecoder(self, substrate, asn1Spec,
                             tagSet=None, length=None, state=None,
                             decodeFun=None, substrateFun=None,
                             **options):
        if tagSet[0].tagFormat != tag.tagFormatConstructed:
            raise error.PyAsn1Error('Constructed tag format expected')

        if substrateFun is not None:
            if asn1Spec is not None:
                asn1Object = asn1Spec.clone()

            elif self.protoComponent is not None:
                asn1Object = self.protoComponent.clone(tagSet=tagSet)

            else:
                asn1Object = self.protoRecordComponent, self.protoSequenceComponent

            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        if asn1Spec is None:
            for asn1Object in self._decodeComponentsSchemaless(
                    substrate, tagSet=tagSet, decodeFun=decodeFun,
                    length=length, **dict(options, allowEoo=True)):
                if isinstance(asn1Object, SubstrateUnderrunError):
                    yield asn1Object

            yield asn1Object

            return

        asn1Object = asn1Spec.clone()
        asn1Object.clear()

        options = self._passAsn1Object(asn1Object, options)

        if asn1Spec.typeId in (univ.Sequence.typeId, univ.Set.typeId):

            namedTypes = asn1Object.componentType

            isSetType = asn1Object.typeId == univ.Set.typeId
            isDeterministic = not isSetType and not namedTypes.hasOptionalOrDefault

            if LOG:
                LOG('decoding %sdeterministic %s type %r chosen by type ID' % (
                    not isDeterministic and 'non-' or '', isSetType and 'SET' or '',
                    asn1Spec))

            seenIndices = set()

            idx = 0

            while True:  # loop over components
                if len(namedTypes) <= idx:
                    asn1Spec = None

                elif isSetType:
                    asn1Spec = namedTypes.tagMapUnique

                else:
                    try:
                        if isDeterministic:
                            asn1Spec = namedTypes[idx].asn1Object

                        elif namedTypes[idx].isOptional or namedTypes[idx].isDefaulted:
                            asn1Spec = namedTypes.getTagMapNearPosition(idx)

                        else:
                            asn1Spec = namedTypes[idx].asn1Object

                    except IndexError:
                        raise error.PyAsn1Error(
                            'Excessive components decoded at %r' % (asn1Object,)
                        )

                for component in decodeFun(substrate, asn1Spec, allowEoo=True, **options):

                    if isinstance(component, SubstrateUnderrunError):
                        yield component

                    if component is eoo.endOfOctets:
                        break

                if component is eoo.endOfOctets:
                    break

                if not isDeterministic and namedTypes:
                    if isSetType:
                        idx = namedTypes.getPositionByType(component.effectiveTagSet)

                    elif namedTypes[idx].isOptional or namedTypes[idx].isDefaulted:
                        idx = namedTypes.getPositionNearType(component.effectiveTagSet, idx)

                asn1Object.setComponentByPosition(
                    idx, component,
                    verifyConstraints=False,
                    matchTags=False, matchConstraints=False
                )

                seenIndices.add(idx)
                idx += 1

            if LOG:
                LOG('seen component indices %s' % seenIndices)

            if namedTypes:
                if not namedTypes.requiredComponents.issubset(seenIndices):
                    raise error.PyAsn1Error(
                        'ASN.1 object %s has uninitialized '
                        'components' % asn1Object.__class__.__name__)

                if namedTypes.hasOpenTypes:

                    openTypes = options.get('openTypes', {})

                    if LOG:
                        LOG('user-specified open types map:')

                        for k, v in openTypes.items():
                            LOG('%s -> %r' % (k, v))

                    if openTypes or options.get('decodeOpenTypes', False):

                        for idx, namedType in enumerate(namedTypes.namedTypes):
                            if not namedType.openType:
                                continue

                            if namedType.isOptional and not asn1Object.getComponentByPosition(idx).isValue:
                                continue

                            governingValue = asn1Object.getComponentByName(
                                namedType.openType.name
                            )

                            try:
                                openType = openTypes[governingValue]

                            except KeyError:

                                if LOG:
                                    LOG('default open types map of component '
                                        '"%s.%s" governed by component "%s.%s"'
                                        ':' % (asn1Object.__class__.__name__,
                                               namedType.name,
                                               asn1Object.__class__.__name__,
                                               namedType.openType.name))

                                    for k, v in namedType.openType.items():
                                        LOG('%s -> %r' % (k, v))

                                try:
                                    openType = namedType.openType[governingValue]

                                except KeyError:
                                    if LOG:
                                        LOG('failed to resolve open type by governing '
                                            'value %r' % (governingValue,))
                                    continue

                            if LOG:
                                LOG('resolved open type %r by governing '
                                    'value %r' % (openType, governingValue))

                            containerValue = asn1Object.getComponentByPosition(idx)

                            if containerValue.typeId in (
                                    univ.SetOf.typeId, univ.SequenceOf.typeId):

                                for pos, containerElement in enumerate(
                                        containerValue):

                                    stream = asSeekableStream(containerValue[pos].asOctets())

                                    for component in decodeFun(stream, asn1Spec=openType,
                                                               **dict(options, allowEoo=True)):
                                        if isinstance(component, SubstrateUnderrunError):
                                            yield component

                                        if component is eoo.endOfOctets:
                                            break

                                    containerValue[pos] = component

                            else:
                                stream = asSeekableStream(asn1Object.getComponentByPosition(idx).asOctets())
                                for component in decodeFun(stream, asn1Spec=openType,
                                                           **dict(options, allowEoo=True)):
                                    if isinstance(component, SubstrateUnderrunError):
                                        yield component

                                    if component is eoo.endOfOctets:
                                        break

                                    asn1Object.setComponentByPosition(idx, component)

                else:
                    inconsistency = asn1Object.isInconsistent
                    if inconsistency:
                        raise inconsistency

        else:
            componentType = asn1Spec.componentType

            if LOG:
                LOG('decoding type %r chosen by given `asn1Spec`' % componentType)

            idx = 0

            while True:

                for component in decodeFun(
                        substrate, componentType, allowEoo=True, **options):

                    if isinstance(component, SubstrateUnderrunError):
                        yield component

                    if component is eoo.endOfOctets:
                        break

                if component is eoo.endOfOctets:
                    break

                asn1Object.setComponentByPosition(
                    idx, component,
                    verifyConstraints=False,
                    matchTags=False, matchConstraints=False
                )

                idx += 1

        yield asn1Object


class SequenceOrSequenceOfPayloadDecoder(ConstructedPayloadDecoderBase):
    protoRecordComponent = univ.Sequence()
    protoSequenceComponent = univ.SequenceOf()


class SequencePayloadDecoder(SequenceOrSequenceOfPayloadDecoder):
    protoComponent = univ.Sequence()


class SequenceOfPayloadDecoder(SequenceOrSequenceOfPayloadDecoder):
    protoComponent = univ.SequenceOf()


class SetOrSetOfPayloadDecoder(ConstructedPayloadDecoderBase):
    protoRecordComponent = univ.Set()
    protoSequenceComponent = univ.SetOf()


class SetPayloadDecoder(SetOrSetOfPayloadDecoder):
    protoComponent = univ.Set()


class SetOfPayloadDecoder(SetOrSetOfPayloadDecoder):
    protoComponent = univ.SetOf()


class ChoicePayloadDecoder(ConstructedPayloadDecoderBase):
    protoComponent = univ.Choice()

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):
        if asn1Spec is None:
            asn1Object = self.protoComponent.clone(tagSet=tagSet)

        else:
            asn1Object = asn1Spec.clone()

        if substrateFun:
            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        options = self._passAsn1Object(asn1Object, options)

        if asn1Object.tagSet == tagSet:
            if LOG:
                LOG('decoding %s as explicitly tagged CHOICE' % (tagSet,))

            for component in decodeFun(
                    substrate, asn1Object.componentTagMap, **options):
                if isinstance(component, SubstrateUnderrunError):
                    yield component

        else:
            if LOG:
                LOG('decoding %s as untagged CHOICE' % (tagSet,))

            for component in decodeFun(
                    substrate, asn1Object.componentTagMap, tagSet, length,
                    state, **options):
                if isinstance(component, SubstrateUnderrunError):
                    yield component

        effectiveTagSet = component.effectiveTagSet

        if LOG:
            LOG('decoded component %s, effective tag set %s' % (component, effectiveTagSet))

        asn1Object.setComponentByType(
            effectiveTagSet, component,
            verifyConstraints=False,
            matchTags=False, matchConstraints=False,
            innerFlag=False
        )

        yield asn1Object

    def indefLenValueDecoder(self, substrate, asn1Spec,
                             tagSet=None, length=None, state=None,
                             decodeFun=None, substrateFun=None,
                             **options):
        if asn1Spec is None:
            asn1Object = self.protoComponent.clone(tagSet=tagSet)

        else:
            asn1Object = asn1Spec.clone()

        if substrateFun:
            for chunk in substrateFun(asn1Object, substrate, length, options):
                yield chunk

            return

        options = self._passAsn1Object(asn1Object, options)

        isTagged = asn1Object.tagSet == tagSet

        if LOG:
            LOG('decoding %s as %stagged CHOICE' % (
                tagSet, isTagged and 'explicitly ' or 'un'))

        while True:

            if isTagged:
                iterator = decodeFun(
                    substrate, asn1Object.componentType.tagMapUnique,
                    **dict(options, allowEoo=True))

            else:
                iterator = decodeFun(
                    substrate, asn1Object.componentType.tagMapUnique,
                    tagSet, length, state, **dict(options, allowEoo=True))

            for component in iterator:

                if isinstance(component, SubstrateUnderrunError):
                    yield component

                if component is eoo.endOfOctets:
                    break

                effectiveTagSet = component.effectiveTagSet

                if LOG:
                    LOG('decoded component %s, effective tag set '
                        '%s' % (component, effectiveTagSet))

                asn1Object.setComponentByType(
                    effectiveTagSet, component,
                    verifyConstraints=False,
                    matchTags=False, matchConstraints=False,
                    innerFlag=False
                )

                if not isTagged:
                    break

            if not isTagged or component is eoo.endOfOctets:
                break

        yield asn1Object


class AnyPayloadDecoder(AbstractSimplePayloadDecoder):
    protoComponent = univ.Any()

    def valueDecoder(self, substrate, asn1Spec,
                     tagSet=None, length=None, state=None,
                     decodeFun=None, substrateFun=None,
                     **options):
        if asn1Spec is None:
            isUntagged = True

        elif asn1Spec.__class__ is tagmap.TagMap:
            isUntagged = tagSet not in asn1Spec.tagMap

        else:
            isUntagged = tagSet != asn1Spec.tagSet

        if isUntagged:
            fullPosition = substrate.markedPosition
            currentPosition = substrate.tell()

            substrate.seek(fullPosition, os.SEEK_SET)
            length += currentPosition - fullPosition

            if LOG:
                for chunk in peekIntoStream(substrate, length):
                    if isinstance(chunk, SubstrateUnderrunError):
                        yield chunk
                LOG('decoding as untagged ANY, substrate '
                    '%s' % debug.hexdump(chunk))

        if substrateFun:
            for chunk in substrateFun(
                    self._createComponent(asn1Spec, tagSet, noValue, **options),
                    substrate, length, options):
                yield chunk

            return

        for chunk in readFromStream(substrate, length, options):
            if isinstance(chunk, SubstrateUnderrunError):
                yield chunk

        yield self._createComponent(asn1Spec, tagSet, chunk, **options)

    def indefLenValueDecoder(self, substrate, asn1Spec,
                             tagSet=None, length=None, state=None,
                             decodeFun=None, substrateFun=None,
                             **options):
        if asn1Spec is None:
            isTagged = False

        elif asn1Spec.__class__ is tagmap.TagMap:
            isTagged = tagSet in asn1Spec.tagMap

        else:
            isTagged = tagSet == asn1Spec.tagSet

        if isTagged:
            # tagged Any type -- consume header substrate
            chunk = null

            if LOG:
                LOG('decoding as tagged ANY')

        else:
            # TODO: Seems not to be tested
            fullPosition = substrate.markedPosition
            currentPosition = substrate.tell()

            substrate.seek(fullPosition, os.SEEK_SET)
            for chunk in readFromStream(substrate, currentPosition - fullPosition, options):
                if isinstance(chunk, SubstrateUnderrunError):
                    yield chunk

            if LOG:
                LOG('decoding as untagged ANY, header substrate %s' % debug.hexdump(chunk))

        # Any components do not inherit initial tag
        asn1Spec = self.protoComponent

        if substrateFun and substrateFun is not self.substrateCollector:
            asn1Object = self._createComponent(
                asn1Spec, tagSet, noValue, **options)

            for chunk in substrateFun(
                    asn1Object, chunk + substrate, length + len(chunk), options):
                yield chunk

            return

        if LOG:
            LOG('assembling constructed serialization')

        # All inner fragments are of the same type, treat them as octet string
        substrateFun = self.substrateCollector

        while True:  # loop over fragments

            for component in decodeFun(
                    substrate, asn1Spec, substrateFun=substrateFun,
                    allowEoo=True, **options):

                if isinstance(component, SubstrateUnderrunError):
                    yield component

                if component is eoo.endOfOctets:
                    break

            if component is eoo.endOfOctets:
                break

            chunk += component

        if substrateFun:
            yield chunk  # TODO: Weird

        else:
            yield self._createComponent(asn1Spec, tagSet, chunk, **options)


# character string types
class UTF8StringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.UTF8String()


class NumericStringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.NumericString()


class PrintableStringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.PrintableString()


class TeletexStringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.TeletexString()


class VideotexStringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.VideotexString()


class IA5StringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.IA5String()


class GraphicStringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.GraphicString()


class VisibleStringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.VisibleString()


class GeneralStringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.GeneralString()


class UniversalStringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.UniversalString()


class BMPStringPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = char.BMPString()


# "useful" types
class ObjectDescriptorPayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = useful.ObjectDescriptor()


class GeneralizedTimePayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = useful.GeneralizedTime()


class UTCTimePayloadDecoder(OctetStringPayloadDecoder):
    protoComponent = useful.UTCTime()


TAG_MAP = {
    univ.Integer.tagSet: IntegerPayloadDecoder(),
    univ.Boolean.tagSet: BooleanPayloadDecoder(),
    univ.BitString.tagSet: BitStringPayloadDecoder(),
    univ.OctetString.tagSet: OctetStringPayloadDecoder(),
    univ.Null.tagSet: NullPayloadDecoder(),
    univ.ObjectIdentifier.tagSet: ObjectIdentifierPayloadDecoder(),
    univ.Enumerated.tagSet: IntegerPayloadDecoder(),
    univ.Real.tagSet: RealPayloadDecoder(),
    univ.Sequence.tagSet: SequenceOrSequenceOfPayloadDecoder(),  # conflicts with SequenceOf
    univ.Set.tagSet: SetOrSetOfPayloadDecoder(),  # conflicts with SetOf
    univ.Choice.tagSet: ChoicePayloadDecoder(),  # conflicts with Any
    # character string types
    char.UTF8String.tagSet: UTF8StringPayloadDecoder(),
    char.NumericString.tagSet: NumericStringPayloadDecoder(),
    char.PrintableString.tagSet: PrintableStringPayloadDecoder(),
    char.TeletexString.tagSet: TeletexStringPayloadDecoder(),
    char.VideotexString.tagSet: VideotexStringPayloadDecoder(),
    char.IA5String.tagSet: IA5StringPayloadDecoder(),
    char.GraphicString.tagSet: GraphicStringPayloadDecoder(),
    char.VisibleString.tagSet: VisibleStringPayloadDecoder(),
    char.GeneralString.tagSet: GeneralStringPayloadDecoder(),
    char.UniversalString.tagSet: UniversalStringPayloadDecoder(),
    char.BMPString.tagSet: BMPStringPayloadDecoder(),
    # useful types
    useful.ObjectDescriptor.tagSet: ObjectDescriptorPayloadDecoder(),
    useful.GeneralizedTime.tagSet: GeneralizedTimePayloadDecoder(),
    useful.UTCTime.tagSet: UTCTimePayloadDecoder()
}

# Type-to-codec map for ambiguous ASN.1 types
TYPE_MAP = {
    univ.Set.typeId: SetPayloadDecoder(),
    univ.SetOf.typeId: SetOfPayloadDecoder(),
    univ.Sequence.typeId: SequencePayloadDecoder(),
    univ.SequenceOf.typeId: SequenceOfPayloadDecoder(),
    univ.Choice.typeId: ChoicePayloadDecoder(),
    univ.Any.typeId: AnyPayloadDecoder()
}

# Put in non-ambiguous types for faster codec lookup
for typeDecoder in TAG_MAP.values():
    if typeDecoder.protoComponent is not None:
        typeId = typeDecoder.protoComponent.__class__.typeId
        if typeId is not None and typeId not in TYPE_MAP:
            TYPE_MAP[typeId] = typeDecoder


(stDecodeTag,
 stDecodeLength,
 stGetValueDecoder,
 stGetValueDecoderByAsn1Spec,
 stGetValueDecoderByTag,
 stTryAsExplicitTag,
 stDecodeValue,
 stDumpRawValue,
 stErrorCondition,
 stStop) = [x for x in range(10)]


EOO_SENTINEL = ints2octs((0, 0))


class SingleItemDecoder(object):
    defaultErrorState = stErrorCondition
    #defaultErrorState = stDumpRawValue
    defaultRawDecoder = AnyPayloadDecoder()

    supportIndefLength = True

    TAG_MAP = TAG_MAP
    TYPE_MAP = TYPE_MAP

    def __init__(self, **options):
        self._tagMap = options.get('tagMap', self.TAG_MAP)
        self._typeMap = options.get('typeMap', self.TYPE_MAP)

        # Tag & TagSet objects caches
        self._tagCache = {}
        self._tagSetCache = {}

    def __call__(self, substrate, asn1Spec=None,
                 tagSet=None, length=None, state=stDecodeTag,
                 decodeFun=None, substrateFun=None,
                 **options):

        allowEoo = options.pop('allowEoo', False)

        if LOG:
            LOG('decoder called at scope %s with state %d, working with up '
                'to %s octets of substrate: '
                '%s' % (debug.scope, state, length, substrate))

        # Look for end-of-octets sentinel
        if allowEoo and self.supportIndefLength:

            for eoo_candidate in readFromStream(substrate, 2, options):
                if isinstance(eoo_candidate, SubstrateUnderrunError):
                    yield eoo_candidate

            if eoo_candidate == EOO_SENTINEL:
                if LOG:
                    LOG('end-of-octets sentinel found')
                yield eoo.endOfOctets
                return

            else:
                substrate.seek(-2, os.SEEK_CUR)

        tagMap = self._tagMap
        typeMap = self._typeMap
        tagCache = self._tagCache
        tagSetCache = self._tagSetCache

        value = noValue

        substrate.markedPosition = substrate.tell()

        while state is not stStop:

            if state is stDecodeTag:
                # Decode tag
                isShortTag = True

                for firstByte in readFromStream(substrate, 1, options):
                    if isinstance(firstByte, SubstrateUnderrunError):
                        yield firstByte

                firstOctet = ord(firstByte)

                try:
                    lastTag = tagCache[firstOctet]

                except KeyError:
                    integerTag = firstOctet
                    tagClass = integerTag & 0xC0
                    tagFormat = integerTag & 0x20
                    tagId = integerTag & 0x1F

                    if tagId == 0x1F:
                        isShortTag = False
                        lengthOctetIdx = 0
                        tagId = 0

                        while True:
                            for integerByte in readFromStream(substrate, 1, options):
                                if isinstance(integerByte, SubstrateUnderrunError):
                                    yield integerByte

                            if not integerByte:
                                raise error.SubstrateUnderrunError(
                                    'Short octet stream on long tag decoding'
                                )

                            integerTag = ord(integerByte)
                            lengthOctetIdx += 1
                            tagId <<= 7
                            tagId |= (integerTag & 0x7F)

                            if not integerTag & 0x80:
                                break

                    lastTag = tag.Tag(
                        tagClass=tagClass, tagFormat=tagFormat, tagId=tagId
                    )

                    if isShortTag:
                        # cache short tags
                        tagCache[firstOctet] = lastTag

                if tagSet is None:
                    if isShortTag:
                        try:
                            tagSet = tagSetCache[firstOctet]

                        except KeyError:
                            # base tag not recovered
                            tagSet = tag.TagSet((), lastTag)
                            tagSetCache[firstOctet] = tagSet
                    else:
                        tagSet = tag.TagSet((), lastTag)

                else:
                    tagSet = lastTag + tagSet

                state = stDecodeLength

                if LOG:
                    LOG('tag decoded into %s, decoding length' % tagSet)

            if state is stDecodeLength:
                # Decode length
                for firstOctet in readFromStream(substrate, 1, options):
                    if isinstance(firstOctet, SubstrateUnderrunError):
                        yield firstOctet

                firstOctet = ord(firstOctet)

                if firstOctet < 128:
                    length = firstOctet

                elif firstOctet > 128:
                    size = firstOctet & 0x7F
                    # encoded in size bytes
                    for encodedLength in readFromStream(substrate, size, options):
                        if isinstance(encodedLength, SubstrateUnderrunError):
                            yield encodedLength
                    encodedLength = list(encodedLength)
                    # missing check on maximum size, which shouldn't be a
                    # problem, we can handle more than is possible
                    if len(encodedLength) != size:
                        raise error.SubstrateUnderrunError(
                            '%s<%s at %s' % (size, len(encodedLength), tagSet)
                        )

                    length = 0
                    for lengthOctet in encodedLength:
                        length <<= 8
                        length |= oct2int(lengthOctet)
                    size += 1

                else:  # 128 means indefinite
                    length = -1

                if length == -1 and not self.supportIndefLength:
                    raise error.PyAsn1Error('Indefinite length encoding not supported by this codec')

                state = stGetValueDecoder

                if LOG:
                    LOG('value length decoded into %d' % length)

            if state is stGetValueDecoder:
                if asn1Spec is None:
                    state = stGetValueDecoderByTag

                else:
                    state = stGetValueDecoderByAsn1Spec
            #
            # There're two ways of creating subtypes in ASN.1 what influences
            # decoder operation. These methods are:
            # 1) Either base types used in or no IMPLICIT tagging has been
            #    applied on subtyping.
            # 2) Subtype syntax drops base type information (by means of
            #    IMPLICIT tagging.
            # The first case allows for complete tag recovery from substrate
            # while the second one requires original ASN.1 type spec for
            # decoding.
            #
            # In either case a set of tags (tagSet) is coming from substrate
            # in an incremental, tag-by-tag fashion (this is the case of
            # EXPLICIT tag which is most basic). Outermost tag comes first
            # from the wire.
            #
            if state is stGetValueDecoderByTag:
                try:
                    concreteDecoder = tagMap[tagSet]

                except KeyError:
                    concreteDecoder = None

                if concreteDecoder:
                    state = stDecodeValue

                else:
                    try:
                        concreteDecoder = tagMap[tagSet[:1]]

                    except KeyError:
                        concreteDecoder = None

                    if concreteDecoder:
                        state = stDecodeValue
                    else:
                        state = stTryAsExplicitTag

                if LOG:
                    LOG('codec %s chosen by a built-in type, decoding %s' % (concreteDecoder and concreteDecoder.__class__.__name__ or "<none>", state is stDecodeValue and 'value' or 'as explicit tag'))
                    debug.scope.push(concreteDecoder is None and '?' or concreteDecoder.protoComponent.__class__.__name__)

            if state is stGetValueDecoderByAsn1Spec:

                if asn1Spec.__class__ is tagmap.TagMap:
                    try:
                        chosenSpec = asn1Spec[tagSet]

                    except KeyError:
                        chosenSpec = None

                    if LOG:
                        LOG('candidate ASN.1 spec is a map of:')

                        for firstOctet, v in asn1Spec.presentTypes.items():
                            LOG('  %s -> %s' % (firstOctet, v.__class__.__name__))

                        if asn1Spec.skipTypes:
                            LOG('but neither of: ')
                            for firstOctet, v in asn1Spec.skipTypes.items():
                                LOG('  %s -> %s' % (firstOctet, v.__class__.__name__))
                        LOG('new candidate ASN.1 spec is %s, chosen by %s' % (chosenSpec is None and '<none>' or chosenSpec.prettyPrintType(), tagSet))

                elif tagSet == asn1Spec.tagSet or tagSet in asn1Spec.tagMap:
                    chosenSpec = asn1Spec
                    if LOG:
                        LOG('candidate ASN.1 spec is %s' % asn1Spec.__class__.__name__)

                else:
                    chosenSpec = None

                if chosenSpec is not None:
                    try:
                        # ambiguous type or just faster codec lookup
                        concreteDecoder = typeMap[chosenSpec.typeId]

                        if LOG:
                            LOG('value decoder chosen for an ambiguous type by type ID %s' % (chosenSpec.typeId,))

                    except KeyError:
                        # use base type for codec lookup to recover untagged types
                        baseTagSet = tag.TagSet(chosenSpec.tagSet.baseTag,  chosenSpec.tagSet.baseTag)
                        try:
                            # base type or tagged subtype
                            concreteDecoder = tagMap[baseTagSet]

                            if LOG:
                                LOG('value decoder chosen by base %s' % (baseTagSet,))

                        except KeyError:
                            concreteDecoder = None

                    if concreteDecoder:
                        asn1Spec = chosenSpec
                        state = stDecodeValue

                    else:
                        state = stTryAsExplicitTag

                else:
                    concreteDecoder = None
                    state = stTryAsExplicitTag

                if LOG:
                    LOG('codec %s chosen by ASN.1 spec, decoding %s' % (state is stDecodeValue and concreteDecoder.__class__.__name__ or "<none>", state is stDecodeValue and 'value' or 'as explicit tag'))
                    debug.scope.push(chosenSpec is None and '?' or chosenSpec.__class__.__name__)

            if state is stDecodeValue:
                if not options.get('recursiveFlag', True) and not substrateFun:  # deprecate this
                    substrateFun = lambda a, b, c: (a, b[:c])

                original_position = substrate.tell()

                if length == -1:  # indef length
                    for value in concreteDecoder.indefLenValueDecoder(
                            substrate, asn1Spec,
                            tagSet, length, stGetValueDecoder,
                            self, substrateFun, **options):
                        if isinstance(value, SubstrateUnderrunError):
                            yield value

                else:
                    for value in concreteDecoder.valueDecoder(
                            substrate, asn1Spec,
                            tagSet, length, stGetValueDecoder,
                            self, substrateFun, **options):
                        if isinstance(value, SubstrateUnderrunError):
                            yield value

                    bytesRead = substrate.tell() - original_position
                    if bytesRead != length:
                        raise PyAsn1Error(
                            "Read %s bytes instead of expected %s." % (bytesRead, length))

                if LOG:
                   LOG('codec %s yields type %s, value:\n%s\n...' % (
                       concreteDecoder.__class__.__name__, value.__class__.__name__,
                       isinstance(value, base.Asn1Item) and value.prettyPrint() or value))

                state = stStop
                break

            if state is stTryAsExplicitTag:
                if (tagSet and
                        tagSet[0].tagFormat == tag.tagFormatConstructed and
                        tagSet[0].tagClass != tag.tagClassUniversal):
                    # Assume explicit tagging
                    concreteDecoder = rawPayloadDecoder
                    state = stDecodeValue

                else:
                    concreteDecoder = None
                    state = self.defaultErrorState

                if LOG:
                    LOG('codec %s chosen, decoding %s' % (concreteDecoder and concreteDecoder.__class__.__name__ or "<none>", state is stDecodeValue and 'value' or 'as failure'))

            if state is stDumpRawValue:
                concreteDecoder = self.defaultRawDecoder

                if LOG:
                    LOG('codec %s chosen, decoding value' % concreteDecoder.__class__.__name__)

                state = stDecodeValue

            if state is stErrorCondition:
                raise error.PyAsn1Error(
                    '%s not in asn1Spec: %r' % (tagSet, asn1Spec)
                )

        if LOG:
            debug.scope.pop()
            LOG('decoder left scope %s, call completed' % debug.scope)

        yield value


class StreamingDecoder(object):
    """Create an iterator that turns BER/CER/DER byte stream into ASN.1 objects.

    On each iteration, consume whatever BER/CER/DER serialization is
    available in the `substrate` stream-like object and turns it into
    one or more, possibly nested, ASN.1 objects.

    Parameters
    ----------
    substrate: :py:class:`file`, :py:class:`io.BytesIO`
        BER/CER/DER serialization in form of a byte stream

    Keyword Args
    ------------
    asn1Spec: :py:class:`~pyasn1.type.base.PyAsn1Item`
        A pyasn1 type object to act as a template guiding the decoder.
        Depending on the ASN.1 structure being decoded, `asn1Spec` may
        or may not be required. One of the reasons why `asn1Spec` may
        me required is that ASN.1 structure is encoded in the *IMPLICIT*
        tagging mode.

    Yields
    ------
    : :py:class:`~pyasn1.type.base.PyAsn1Item`, :py:class:`~pyasn1.error.SubstrateUnderrunError`
        Decoded ASN.1 object (possibly, nested) or
        :py:class:`~pyasn1.error.SubstrateUnderrunError` object indicating
        insufficient BER/CER/DER serialization on input to fully recover ASN.1
        objects from it.
        
        In the latter case the caller is advised to ensure some more data in
        the input stream, then call the iterator again. The decoder will resume
        the decoding process using the newly arrived data.

        The `context` property of :py:class:`~pyasn1.error.SubstrateUnderrunError`
        object might hold a reference to the partially populated ASN.1 object
        being reconstructed.

    Raises
    ------
    ~pyasn1.error.PyAsn1Error, ~pyasn1.error.EndOfStreamError
        `PyAsn1Error` on deserialization error, `EndOfStreamError` on
         premature stream closure.

    Examples
    --------
    Decode BER serialisation without ASN.1 schema

    .. code-block:: pycon

        >>> stream = io.BytesIO(
        ...    b'0\t\x02\x01\x01\x02\x01\x02\x02\x01\x03')
        >>>
        >>> for asn1Object in StreamingDecoder(stream):
        ...     print(asn1Object)
        >>>
        SequenceOf:
         1 2 3

    Decode BER serialisation with ASN.1 schema

    .. code-block:: pycon

        >>> stream = io.BytesIO(
        ...    b'0\t\x02\x01\x01\x02\x01\x02\x02\x01\x03')
        >>>
        >>> schema = SequenceOf(componentType=Integer())
        >>>
        >>> decoder = StreamingDecoder(stream, asn1Spec=schema)
        >>> for asn1Object in decoder:
        ...     print(asn1Object)
        >>>
        SequenceOf:
         1 2 3
    """

    SINGLE_ITEM_DECODER = SingleItemDecoder

    def __init__(self, substrate, asn1Spec=None, **options):
        self._singleItemDecoder = self.SINGLE_ITEM_DECODER(**options)
        self._substrate = asSeekableStream(substrate)
        self._asn1Spec = asn1Spec
        self._options = options

    def __iter__(self):
        while True:
            for asn1Object in self._singleItemDecoder(
                    self._substrate, self._asn1Spec, **self._options):
                yield asn1Object

            for chunk in isEndOfStream(self._substrate):
                if isinstance(chunk, SubstrateUnderrunError):
                    yield

                break

            if chunk:
                break


class Decoder(object):
    """Create a BER decoder object.

    Parse BER/CER/DER octet-stream into one, possibly nested, ASN.1 object.
    """
    STREAMING_DECODER = StreamingDecoder

    @classmethod
    def __call__(cls, substrate, asn1Spec=None, **options):
        """Turns BER/CER/DER octet stream into an ASN.1 object.

        Takes BER/CER/DER octet-stream in form of :py:class:`bytes` (Python 3)
        or :py:class:`str` (Python 2) and decode it into an ASN.1 object
        (e.g. :py:class:`~pyasn1.type.base.PyAsn1Item` derivative) which
        may be a scalar or an arbitrary nested structure.

        Parameters
        ----------
        substrate: :py:class:`bytes` (Python 3) or :py:class:`str` (Python 2)
            BER/CER/DER octet-stream to parse

        Keyword Args
        ------------
        asn1Spec: :py:class:`~pyasn1.type.base.PyAsn1Item`
            A pyasn1 type object (:py:class:`~pyasn1.type.base.PyAsn1Item`
            derivative) to act as a template guiding the decoder.
            Depending on the ASN.1 structure being decoded, `asn1Spec` may or
            may not be required. Most common reason for it to require is that
            ASN.1 structure is encoded in *IMPLICIT* tagging mode.

        Returns
        -------
        : :py:class:`tuple`
            A tuple of :py:class:`~pyasn1.type.base.PyAsn1Item` object
            recovered from BER/CER/DER substrate and the unprocessed trailing
            portion of the `substrate` (may be empty)

        Raises
        ------
        : :py:class:`~pyasn1.error.PyAsn1Error`
            :py:class:`~pyasn1.error.SubstrateUnderrunError` on insufficient
            input or :py:class:`~pyasn1.error.PyAsn1Error` on decoding error.

        Examples
        --------
        Decode BER/CER/DER serialisation without ASN.1 schema

        .. code-block:: pycon

           >>> s, unprocessed = decode(b'0\t\x02\x01\x01\x02\x01\x02\x02\x01\x03')
           >>> str(s)
           SequenceOf:
            1 2 3

        Decode BER/CER/DER serialisation with ASN.1 schema

        .. code-block:: pycon

           >>> seq = SequenceOf(componentType=Integer())
           >>> s, unprocessed = decode(
                b'0\t\x02\x01\x01\x02\x01\x02\x02\x01\x03', asn1Spec=seq)
           >>> str(s)
           SequenceOf:
            1 2 3

        """
        substrate = asSeekableStream(substrate)

        streamingDecoder = cls.STREAMING_DECODER(
            substrate, asn1Spec, **options)

        for asn1Object in streamingDecoder:
            if isinstance(asn1Object, SubstrateUnderrunError):
                raise error.SubstrateUnderrunError('Short substrate on input')

            try:
                tail = next(readFromStream(substrate))

            except error.EndOfStreamError:
                tail = null

            return asn1Object, tail


#: Turns BER octet stream into an ASN.1 object.
#:
#: Takes BER octet-stream and decode it into an ASN.1 object
#: (e.g. :py:class:`~pyasn1.type.base.PyAsn1Item` derivative) which
#: may be a scalar or an arbitrary nested structure.
#:
#: Parameters
#: ----------
#: substrate: :py:class:`bytes` (Python 3) or :py:class:`str` (Python 2)
#:     BER octet-stream
#:
#: Keyword Args
#: ------------
#: asn1Spec: any pyasn1 type object e.g. :py:class:`~pyasn1.type.base.PyAsn1Item` derivative
#:     A pyasn1 type object to act as a template guiding the decoder. Depending on the ASN.1 structure
#:     being decoded, *asn1Spec* may or may not be required. Most common reason for
#:     it to require is that ASN.1 structure is encoded in *IMPLICIT* tagging mode.
#:
#: Returns
#: -------
#: : :py:class:`tuple`
#:     A tuple of pyasn1 object recovered from BER substrate (:py:class:`~pyasn1.type.base.PyAsn1Item` derivative)
#:     and the unprocessed trailing portion of the *substrate* (may be empty)
#:
#: Raises
#: ------
#: ~pyasn1.error.PyAsn1Error, ~pyasn1.error.SubstrateUnderrunError
#:     On decoding errors
#:
#: Notes
#: -----
#: This function is deprecated. Please use :py:class:`Decoder` or
#: :py:class:`StreamingDecoder` class instance.
#:
#: Examples
#: --------
#: Decode BER serialisation without ASN.1 schema
#:
#: .. code-block:: pycon
#:
#:    >>> s, _ = decode(b'0\t\x02\x01\x01\x02\x01\x02\x02\x01\x03')
#:    >>> str(s)
#:    SequenceOf:
#:     1 2 3
#:
#: Decode BER serialisation with ASN.1 schema
#:
#: .. code-block:: pycon
#:
#:    >>> seq = SequenceOf(componentType=Integer())
#:    >>> s, _ = decode(b'0\t\x02\x01\x01\x02\x01\x02\x02\x01\x03', asn1Spec=seq)
#:    >>> str(s)
#:    SequenceOf:
#:     1 2 3
#:
decode = Decoder()
